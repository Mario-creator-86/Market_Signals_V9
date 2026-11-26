"""Deterministic multi-factor champion/challenger promotion policy."""

from __future__ import annotations

import hashlib
import json

from .types import PromotionDecision, PromotionEvidence, PromotionThresholds


_REASONS = {
    "oos_verdict": "oos_verdict_not_promoting",
    "oos_sample": "oos_sample_below_minimum",
    "oos_expectancy": "oos_expectancy_not_positive",
    "oos_profit_factor": "oos_profit_factor_below_minimum",
    "oos_drawdown": "oos_drawdown_exceeded",
    "oos_sharpe": "oos_sharpe_below_minimum",
    "parameter_stability": "parameters_unstable",
    "costs_included": "costs_not_included",
    "regime_coverage": "regime_coverage_below_minimum",
    "recent_sample": "recent_sample_below_minimum",
    "recent_expectancy": "recent_expectancy_not_positive",
    "shadow_sample": "shadow_sample_below_minimum",
    "shadow_expectancy": "shadow_expectancy_not_positive",
    "shadow_drawdown": "shadow_drawdown_exceeded",
    "final_holdout_untouched": "final_holdout_was_optimized",
    "evaluation_period_locked": "evaluation_period_not_locked",
    "promotion_criteria_locked": "promotion_criteria_not_locked",
    "champion_oos_comparison": "challenger_does_not_beat_champion_oos",
    "champion_shadow_comparison": "challenger_does_not_beat_champion_shadow",
}


def fingerprint_promotion_evidence(
    evidence: PromotionEvidence,
    thresholds: PromotionThresholds,
) -> str:
    """Return a canonical fingerprint binding evidence to predefined policy."""

    payload = {
        "evidence": evidence.to_dict(),
        "thresholds": thresholds.to_dict(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_promotion(
    evidence: PromotionEvidence,
    *,
    thresholds: PromotionThresholds = PromotionThresholds(),
    champion_exists: bool = False,
) -> PromotionDecision:
    """Evaluate every §63 gate; no single metric can authorize promotion."""

    checks = {
        "oos_verdict": evidence.oos_verdict in {"ROBUST", "MODERATE"},
        "oos_sample": evidence.oos_sample_size >= thresholds.min_oos_sample,
        "oos_expectancy": evidence.oos_expectancy_r > thresholds.min_oos_expectancy_r,
        "oos_profit_factor": evidence.oos_profit_factor >= thresholds.min_oos_profit_factor,
        "oos_drawdown": evidence.oos_max_drawdown_pct <= thresholds.max_oos_drawdown_pct,
        "oos_sharpe": evidence.oos_sharpe >= thresholds.min_oos_sharpe,
        "parameter_stability": evidence.parameter_stable is True,
        "costs_included": evidence.costs_included is True,
        "regime_coverage": evidence.regime_count >= thresholds.min_regime_count,
        "recent_sample": evidence.recent_sample_size >= thresholds.min_recent_sample,
        "recent_expectancy": evidence.recent_expectancy_r > thresholds.min_recent_expectancy_r,
        "shadow_sample": evidence.shadow_sample_size >= thresholds.min_shadow_sample,
        "shadow_expectancy": evidence.shadow_expectancy_r > thresholds.min_shadow_expectancy_r,
        "shadow_drawdown": evidence.shadow_max_drawdown_pct <= thresholds.max_shadow_drawdown_pct,
        "final_holdout_untouched": evidence.final_holdout_untouched is True,
        "evaluation_period_locked": evidence.evaluation_period_locked is True,
        "promotion_criteria_locked": evidence.promotion_criteria_locked is True,
    }

    comparison_reasons: list[str] = []
    if champion_exists:
        if evidence.champion_oos_expectancy_r is None:
            checks["champion_oos_comparison"] = False
            comparison_reasons.append("champion_oos_comparison_missing")
        else:
            checks["champion_oos_comparison"] = (
                evidence.oos_expectancy_r
                > evidence.champion_oos_expectancy_r + thresholds.min_champion_improvement_r
            )
        if evidence.champion_shadow_expectancy_r is None:
            checks["champion_shadow_comparison"] = False
            comparison_reasons.append("champion_shadow_comparison_missing")
        else:
            checks["champion_shadow_comparison"] = (
                evidence.shadow_expectancy_r
                > evidence.champion_shadow_expectancy_r + thresholds.min_champion_improvement_r
            )
    else:
        checks["champion_oos_comparison"] = True
        checks["champion_shadow_comparison"] = True

    reasons = [
        _REASONS[name]
        for name, passed in checks.items()
        if not passed and name in _REASONS
    ]
    for reason in comparison_reasons:
        comparison_gate = "champion_oos_comparison" if "oos" in reason else "champion_shadow_comparison"
        generic = _REASONS[comparison_gate]
        if generic in reasons:
            reasons.remove(generic)
        reasons.append(reason)
    approved = all(checks.values())
    return PromotionDecision(
        approved=approved,
        reasons=tuple(reasons),
        checks=checks,
        evidence_fingerprint=fingerprint_promotion_evidence(evidence, thresholds),
    )
