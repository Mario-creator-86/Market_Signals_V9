"""Pre-Earnings evidence lab: learn from settled outcomes, verify every claim.

Every prediction the product makes (heuristic direction score, AI verdict,
Top 10 direction, gap-size probability, learned models) is evaluated
walk-forward — rules and models are fitted only on events that settled
*before* the test month — against a naive baseline on the *same* events.
A claim is labelled EDGE_PROVEN only when a one-sided paired sign test
(direction) or a bootstrap confidence interval (gap size) shows it beats
that baseline.

Learned models run in SHADOW: their predictions are frozen onto the open
prediction before the event and settle like any other call. That forward
record is the evidence that can later justify promotion. Nothing here places
orders or changes trading weights.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from core.estimate_revisions import revision_direction
from core.pre_event_features import REVERSAL_HYPOTHESIS_REGISTERED_UTC, reversal_direction

SCHEMA_VERSION = 1
MODEL_FAMILY = "pre_earnings_learning_v1"
MIN_TRAIN_EVENTS = 300
MIN_CLAIM_EVENTS = 100
SIGNIFICANCE = 0.05
MAX_PLAUSIBLE_MOVE_PCT = 100.0

EDGE_PROVEN = "EDGE_PROVEN"
NO_EDGE = "NO_EDGE"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

DIRECTION_FEATURES = (
    "direction_bias", "ai_signed_confidence", "gap_risk", "session_bmo",
    "cap_size", "news_intensity", "historical_move", "catalyst_severity",
)
_CAP_SIZE = {"MEGA": 1.0, "LARGE": 0.75, "MID": 0.5, "SMALL": 0.25, "MICRO": 0.0}

SHADOW_FIELDS = (
    "learning_model_version", "shadow_direction_prob_up", "shadow_prior_up_rate",
    "shadow_prob_high_gap", "shadow_prior_high_gap_rate",
)


# --------------------------------------------------------------------------- utils
def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def wilson_interval(hits: int, n: int, z: float = 1.96) -> Optional[Tuple[float, float]]:
    if n <= 0:
        return None
    p = hits / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (round(100 * (centre - half), 2), round(100 * (centre + half), 2))


def binomial_upper_tail(successes: int, trials: int) -> float:
    """P(X >= successes) for X ~ Binomial(trials, 0.5): one-sided sign test."""
    if trials <= 0:
        return 1.0
    log_half = trials * math.log(0.5)
    total = 0.0
    for k in range(successes, trials + 1):
        total += math.exp(
            math.lgamma(trials + 1) - math.lgamma(k + 1) - math.lgamma(trials - k + 1) + log_half
        )
    return min(1.0, total)


def _average_ranks(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    sorted_vals = values[order]
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def roc_auc(scores: Sequence[float], labels: Sequence[float]) -> Optional[float]:
    """Tie-aware AUC (average ranks)."""
    y = np.asarray(labels, dtype=float)
    positives = float(y.sum())
    negatives = float(len(y) - positives)
    if positives == 0 or negatives == 0:
        return None
    ranks = _average_ranks(np.asarray(scores, dtype=float))
    return float((ranks[y == 1].sum() - positives * (positives + 1) / 2) / (positives * negatives))


# ---------------------------------------------------------------------- dataset
def validate_outcomes(outcomes: Iterable[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Keep outcomes whose target is trustworthy; count every exclusion reason.

    Input rows are never modified; excluded rows stay in the store for review.
    """
    exclusions: Counter = Counter()
    by_key: Dict[str, Dict[str, Any]] = {}
    for raw in outcomes or []:
        if not isinstance(raw, Mapping):
            exclusions["malformed_row"] += 1
            continue
        move = _finite(raw.get("realized_signed_move_pct"))
        event = _parse_utc(raw.get("event_utc"))
        evaluated = _parse_utc(raw.get("evaluation_utc"))
        session = str(raw.get("session") or "").upper()
        if move is None:
            exclusions["missing_realized_move"] += 1
            continue
        if event is None:
            exclusions["missing_event_time"] += 1
            continue
        if evaluated is not None and evaluated < event:
            exclusions["settled_before_event"] += 1
            continue
        if session not in {"BMO", "AMC"}:
            exclusions["session_unknown"] += 1
            continue
        if abs(move) > MAX_PLAUSIBLE_MOVE_PCT:
            exclusions["implausible_move_needs_manual_review"] += 1
            continue
        key = str(raw.get("prediction_key") or f"{raw.get('symbol')}|{raw.get('event_utc')}")
        if key in by_key:
            exclusions["duplicate_event_kept_latest"] += 1
        row = dict(raw)
        row["_event_dt"] = event
        row["_month"] = event.strftime("%Y-%m")
        by_key[key] = row
    rows = sorted(by_key.values(), key=lambda r: r["_event_dt"])
    return rows, dict(exclusions)


def feature_vector(record: Mapping[str, Any]) -> List[float]:
    """Pre-event features only. Works on open predictions and settled outcomes."""
    contributors = record.get("contributors") if isinstance(record.get("contributors"), Mapping) else {}
    verdict = str(record.get("predicted_ai_verdict") or "").upper()
    ai_sign = 1.0 if verdict == "LONG" else (-1.0 if verdict == "SHORT" else 0.0)
    confidence = _finite(record.get("predicted_ai_confidence")) or 0.0
    return [
        (_finite(record.get("predicted_direction_bias")) or 0.0) / 100.0,
        ai_sign * confidence,
        (_finite(record.get("predicted_gap_risk")) or 0.0) / 100.0,
        1.0 if str(record.get("session") or "").upper() == "BMO" else 0.0,
        _CAP_SIZE.get(str(record.get("cap_bucket") or "").upper(), 0.25),
        _finite(contributors.get("news_intensity")) or 0.0,
        _finite(contributors.get("historical_move")) or 0.0,
        _finite(contributors.get("catalyst_severity")) or 0.0,
    ]


def _went_up(row: Mapping[str, Any]) -> Optional[float]:
    move = _finite(row.get("realized_signed_move_pct"))
    if move is None or move == 0.0:
        return None
    return 1.0 if move > 0 else 0.0


def _monthly_folds(
    rows: Sequence[Dict[str, Any]], min_train: int
) -> List[Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]]:
    months = sorted({row["_month"] for row in rows})
    folds = []
    for month in months:
        train = [row for row in rows if row["_month"] < month]
        test = [row for row in rows if row["_month"] == month]
        if len(train) >= min_train and test:
            folds.append((month, train, test))
    return folds


# ------------------------------------------------------------------------ models
def fit_logistic(X: np.ndarray, y: np.ndarray, *, l2: float = 1.0, iterations: int = 25) -> Dict[str, Any]:
    """L2-regularised logistic regression (Newton) on standardised features."""
    means = X.mean(axis=0)
    stds = X.std(axis=0)
    stds[stds == 0] = 1.0
    Z = np.c_[np.ones(len(X)), (X - means) / stds]
    w = np.zeros(Z.shape[1])
    penalty = np.eye(len(w)) * l2
    penalty[0, 0] = 0.0
    for _ in range(iterations):
        p = 1.0 / (1.0 + np.exp(-np.clip(Z @ w, -30, 30)))
        gradient = Z.T @ (p - y) + penalty @ w
        hessian = (Z.T * (p * (1 - p))) @ Z + penalty + np.eye(len(w)) * 1e-9
        step = np.linalg.solve(hessian, gradient)
        w -= step
        if float(np.abs(step).max()) < 1e-8:
            break
    return {"weights": [float(v) for v in w], "means": [float(v) for v in means], "stds": [float(v) for v in stds]}


def predict_logistic(model: Mapping[str, Any], x: Sequence[float]) -> float:
    w = model["weights"]
    z = w[0] + sum(w[i + 1] * (float(x[i]) - model["means"][i]) / model["stds"][i] for i in range(len(x)))
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def fit_isotonic(scores: Sequence[float], labels: Sequence[float]) -> List[Tuple[float, float]]:
    """Pool-adjacent-violators on ascending scores; tied scores pooled first."""
    grouped: Dict[float, List[float]] = {}
    for score, label in zip(scores, labels):
        grouped.setdefault(float(score), []).append(float(label))
    blocks = [[score, sum(vals) / len(vals), len(vals)] for score, vals in sorted(grouped.items())]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][1] > blocks[i + 1][1]:
            w1, w2 = blocks[i][2], blocks[i + 1][2]
            blocks[i][1] = (blocks[i][1] * w1 + blocks[i + 1][1] * w2) / (w1 + w2)
            blocks[i][2] = w1 + w2
            del blocks[i + 1]
            i = max(0, i - 1)
        else:
            i += 1
    return [(float(b[0]), float(b[1])) for b in blocks]


def apply_isotonic(mapping: Sequence[Sequence[float]], score: float) -> Optional[float]:
    if not mapping:
        return None
    value = float(mapping[0][1])
    for threshold, calibrated in mapping:
        if float(score) >= float(threshold):
            value = float(calibrated)
        else:
            break
    return value


def _stored_gap_probability(row: Mapping[str, Any]) -> Optional[float]:
    if row.get("gap_risk_available") is False:
        return None
    p = _finite(row.get("predicted_prob_high_gap"))
    if p is None:
        risk = _finite(row.get("predicted_gap_risk"))
        p = risk / 100.0 if risk is not None else None
    return None if p is None else min(1.0, max(0.0, p))


# ------------------------------------------------------------------- evaluation
def _paired_comparison(cases: List[Tuple[int, int, int]], baseline_of) -> Dict[str, Any]:
    n = len(cases)
    baseline_hits = sum(1 for case in cases if baseline_of(case) == case[2])
    claim_only = sum(1 for case in cases if case[0] == case[2] and baseline_of(case) != case[2])
    base_only = sum(1 for case in cases if baseline_of(case) == case[2] and case[0] != case[2])
    return {
        "accuracy_pct": round(100 * baseline_hits / n, 2) if n else None,
        "p_value_one_sided": round(binomial_upper_tail(claim_only, claim_only + base_only), 4),
    }


def _direction_claim_summary(name: str, label: str, cases: List[Tuple[int, int, int]]) -> Dict[str, Any]:
    """cases: (claim_side, prior_majority_side, actual_side); side 1 = up, 0 = down.

    A claim must beat EVERY naive baseline on the same events. Always-LONG is
    included because stocks drift up: a LONG-leaning rule with no information
    can otherwise look skilled against a baseline that sometimes picks SHORT.
    """
    n = len(cases)
    hits = sum(1 for claim, _, actual in cases if claim == actual)
    accuracy = round(100 * hits / n, 2) if n else None
    baselines = {
        "prior_majority": _paired_comparison(cases, lambda case: case[1]),
        "always_long": _paired_comparison(cases, lambda case: 1),
    }
    strongest = max(baselines.values(), key=lambda b: -1.0 if b["accuracy_pct"] is None else b["accuracy_pct"])
    edge = round(accuracy - strongest["accuracy_pct"], 2) if n else None
    worst_p = max(b["p_value_one_sided"] for b in baselines.values())
    beats_all = n > 0 and all(
        accuracy > b["accuracy_pct"] and b["p_value_one_sided"] < SIGNIFICANCE for b in baselines.values()
    )
    if n < MIN_CLAIM_EVENTS:
        verdict = INSUFFICIENT_EVIDENCE
    elif beats_all:
        verdict = EDGE_PROVEN
    else:
        verdict = NO_EDGE
    return {
        "claim": name, "label": label, "events": n, "hits": hits,
        "accuracy_pct": accuracy, "accuracy_ci95_pct": wilson_interval(hits, n),
        "baselines": baselines,
        "baseline": "strongest of: majority side of prior settled events, always LONG",
        "baseline_accuracy_pct": strongest["accuracy_pct"],
        "edge_pct_points": edge, "p_value_one_sided": worst_p, "verdict": verdict,
        "long_calls": sum(1 for claim, _, _ in cases if claim == 1),
        "short_calls": sum(1 for claim, _, _ in cases if claim == 0),
    }


NEWS_BACKED_MIN_ARTICLES = 3
_REVERSAL_REGISTERED = datetime.fromisoformat(REVERSAL_HYPOTHESIS_REGISTERED_UTC)

_DIRECTION_LABELS = {
    "heuristic_score": "Direction score (current heuristic)",
    "ai_verdict": "AI verdict LONG/SHORT",
    "ai_verdict_news_backed": f"AI verdict after reading {NEWS_BACKED_MIN_ARTICLES}+ articles",
    "ai_verdict_thin_news": f"AI verdict on fewer than {NEWS_BACKED_MIN_ARTICLES} articles",
    "estimate_revision_30d": "Analyst EPS estimate revisions, 30 days before the event",
    "pre_event_reversal_5d": "Pre-registered: fade a 10%+ 5-day move vs SPY (captured live)",
    "top10_direction": "Top 10 direction",
    "learned_model_walk_forward": "Learned model (walk-forward retrain)",
    "learned_model_live_shadow": "Learned model (live shadow, frozen before event)",
}


def evaluate_direction(rows: Sequence[Dict[str, Any]], *, min_train: int = MIN_TRAIN_EVENTS) -> List[Dict[str, Any]]:
    collected: Dict[str, List[Tuple[int, int, int]]] = {
        "heuristic_score": [], "ai_verdict": [], "ai_verdict_news_backed": [], "ai_verdict_thin_news": [],
        "estimate_revision_30d": [], "pre_event_reversal_5d": [],
        "top10_direction": [], "learned_model_walk_forward": [],
    }
    for _, train, test in _monthly_folds(rows, min_train):
        usable_train = [r for r in train if _went_up(r) is not None]
        if not usable_train:
            continue
        train_labels = [_went_up(r) for r in usable_train]
        baseline_side = 1 if sum(train_labels) >= len(train_labels) / 2 else 0
        model = fit_logistic(
            np.array([feature_vector(r) for r in usable_train], dtype=float),
            np.array(train_labels, dtype=float),
        )
        for row in test:
            actual = _went_up(row)
            if actual is None:
                continue
            actual_side = int(actual)
            bias = _finite(row.get("predicted_direction_bias")) or 0.0
            if bias != 0.0:
                collected["heuristic_score"].append((1 if bias > 0 else 0, baseline_side, actual_side))
            verdict = str(row.get("predicted_ai_verdict") or "").upper()
            if verdict in {"LONG", "SHORT"}:
                ai_case = (1 if verdict == "LONG" else 0, baseline_side, actual_side)
                collected["ai_verdict"].append(ai_case)
                articles_read = _finite(row.get("predicted_news_articles_read"))
                if articles_read is not None:
                    news_key = (
                        "ai_verdict_news_backed"
                        if articles_read >= NEWS_BACKED_MIN_ARTICLES
                        else "ai_verdict_thin_news"
                    )
                    collected[news_key].append(ai_case)
            revision_side = revision_direction(row)
            if revision_side is not None:
                collected["estimate_revision_30d"].append((revision_side, baseline_side, actual_side))
            reversal_side = reversal_direction(row)
            if reversal_side is not None and row["_event_dt"] >= _REVERSAL_REGISTERED:
                collected["pre_event_reversal_5d"].append((reversal_side, baseline_side, actual_side))
            top = str(row.get("top_prediction_direction") or "").upper()
            if row.get("was_top_prediction") and top in {"LONG", "SHORT"}:
                collected["top10_direction"].append((1 if top == "LONG" else 0, baseline_side, actual_side))
            probability = predict_logistic(model, feature_vector(row))
            collected["learned_model_walk_forward"].append(
                (1 if probability >= 0.5 else 0, baseline_side, actual_side)
            )
    summaries = [_direction_claim_summary(key, _DIRECTION_LABELS[key], cases) for key, cases in collected.items()]

    shadow_cases = []
    for row in rows:
        actual = _went_up(row)
        probability = _finite(row.get("shadow_direction_prob_up"))
        if actual is None or probability is None:
            continue
        prior_up = _finite(row.get("shadow_prior_up_rate"))
        baseline_side = 1 if (prior_up if prior_up is not None else 0.5) >= 0.5 else 0
        shadow_cases.append((1 if probability >= 0.5 else 0, baseline_side, int(actual)))
    summaries.append(_direction_claim_summary(
        "learned_model_live_shadow", _DIRECTION_LABELS["learned_model_live_shadow"], shadow_cases
    ))
    return summaries


def _bootstrap_improvement(
    losses_baseline: np.ndarray, losses_claim: np.ndarray, *, resamples: int = 1000
) -> Tuple[float, float]:
    rng = np.random.default_rng(20260915)
    diff = losses_baseline - losses_claim
    means = np.empty(resamples)
    for i in range(resamples):
        means[i] = diff[rng.integers(0, len(diff), size=len(diff))].mean()
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def _gap_claim_summary(
    name: str, label: str, probs: List[float], base: List[float], labels: List[float]
) -> Dict[str, Any]:
    n = len(labels)
    summary: Dict[str, Any] = {
        "claim": name, "label": label, "events": n, "baseline": "base rate of prior settled events",
    }
    if n == 0:
        summary.update({
            "high_gap_rate_pct": None, "brier": None, "baseline_brier": None, "skill_score": None,
            "auc": None, "brier_improvement_ci95": None, "verdict": INSUFFICIENT_EVIDENCE,
        })
        return summary
    p = np.asarray(probs, dtype=float)
    b = np.asarray(base, dtype=float)
    y = np.asarray(labels, dtype=float)
    brier = float(((p - y) ** 2).mean())
    base_brier = float(((b - y) ** 2).mean())
    low, high = _bootstrap_improvement((b - y) ** 2, (p - y) ** 2)
    if n < MIN_CLAIM_EVENTS:
        verdict = INSUFFICIENT_EVIDENCE
    elif low > 0:
        verdict = EDGE_PROVEN
    else:
        verdict = NO_EDGE
    auc = roc_auc(p, y)
    summary.update({
        "high_gap_rate_pct": round(100 * float(y.mean()), 2),
        "brier": round(brier, 5),
        "baseline_brier": round(base_brier, 5),
        "skill_score": round(1 - brier / base_brier, 4) if base_brier > 0 else None,
        "auc": round(auc, 4) if auc is not None else None,
        "brier_improvement_ci95": [round(low, 5), round(high, 5)],
        "verdict": verdict,
    })
    return summary


def evaluate_gap_size(rows: Sequence[Dict[str, Any]], *, min_train: int = MIN_TRAIN_EVENTS) -> List[Dict[str, Any]]:
    stored: Tuple[List[float], List[float], List[float]] = ([], [], [])
    calibrated: Tuple[List[float], List[float], List[float]] = ([], [], [])
    for _, train, test in _monthly_folds(rows, min_train):
        pairs = [(_stored_gap_probability(r), 1.0 if r.get("realized_high_gap") else 0.0) for r in train]
        pairs = [(p, y) for p, y in pairs if p is not None]
        if not pairs:
            continue
        base_rate = sum(y for _, y in pairs) / len(pairs)
        mapping = fit_isotonic([p for p, _ in pairs], [y for _, y in pairs])
        for row in test:
            p = _stored_gap_probability(row)
            if p is None:
                continue
            label = 1.0 if row.get("realized_high_gap") else 0.0
            for bucket, value in ((stored, p), (calibrated, apply_isotonic(mapping, p))):
                bucket[0].append(float(value))
                bucket[1].append(base_rate)
                bucket[2].append(label)

    shadow: Tuple[List[float], List[float], List[float]] = ([], [], [])
    for row in rows:
        p = _finite(row.get("shadow_prob_high_gap"))
        prior = _finite(row.get("shadow_prior_high_gap_rate"))
        if p is None or prior is None:
            continue
        shadow[0].append(p)
        shadow[1].append(prior)
        shadow[2].append(1.0 if row.get("realized_high_gap") else 0.0)
    return [
        _gap_claim_summary("stored_gap_probability", "Gap risk probability (current)", *stored),
        _gap_claim_summary(
            "calibrated_gap_probability_walk_forward",
            "Gap risk, calibrated on prior events (walk-forward)", *calibrated,
        ),
        _gap_claim_summary(
            "calibrated_gap_probability_live_shadow",
            "Gap risk, calibrated (live shadow, frozen before event)", *shadow,
        ),
    ]


# ----------------------------------------------------------- production (shadow)
def fit_learning_model(
    outcomes: Iterable[Mapping[str, Any]], *, trained_at: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Fit on every validated settled outcome. Used only for shadow predictions."""
    rows, exclusions = validate_outcomes(outcomes)
    direction_rows = [r for r in rows if _went_up(r) is not None]
    if len(direction_rows) < MIN_TRAIN_EVENTS:
        return None
    X = np.array([feature_vector(r) for r in direction_rows], dtype=float)
    y = np.array([_went_up(r) for r in direction_rows], dtype=float)
    gap_pairs = [(_stored_gap_probability(r), 1.0 if r.get("realized_high_gap") else 0.0) for r in rows]
    gap_pairs = [(p, label) for p, label in gap_pairs if p is not None]
    last_event = rows[-1]["_event_dt"].isoformat()
    return {
        "family": MODEL_FAMILY,
        "version": f"{MODEL_FAMILY}@{last_event[:10]}#{len(rows)}",
        "trained_at_utc": trained_at or datetime.now(timezone.utc).isoformat(),
        "training_events": len(rows),
        "training_last_event_utc": last_event,
        "excluded": exclusions,
        "direction": {"features": list(DIRECTION_FEATURES), **fit_logistic(X, y)},
        "prior_up_rate": round(float(y.mean()), 6),
        "gap": {
            "mapping": fit_isotonic([p for p, _ in gap_pairs], [label for _, label in gap_pairs])
            if gap_pairs else []
        },
        "prior_high_gap_rate": (
            round(sum(label for _, label in gap_pairs) / len(gap_pairs), 6) if gap_pairs else None
        ),
    }


def shadow_predictions(model: Optional[Mapping[str, Any]], record: Mapping[str, Any]) -> Dict[str, Any]:
    """Fields frozen onto an open prediction. Empty when no model exists."""
    if not isinstance(model, Mapping) or not isinstance(model.get("direction"), Mapping):
        return {}
    try:
        out: Dict[str, Any] = {
            "learning_model_version": model.get("version"),
            "shadow_direction_prob_up": round(predict_logistic(model["direction"], feature_vector(record)), 6),
            "shadow_prior_up_rate": model.get("prior_up_rate"),
        }
        gap_p = _stored_gap_probability(record)
        mapping = (model.get("gap") or {}).get("mapping") or []
        calibrated = apply_isotonic(mapping, gap_p) if gap_p is not None else None
        if calibrated is not None and model.get("prior_high_gap_rate") is not None:
            out["shadow_prob_high_gap"] = round(calibrated, 6)
            out["shadow_prior_high_gap_rate"] = model.get("prior_high_gap_rate")
        return out
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
        return {}


# ------------------------------------------------------------ multiple testing
def apply_holm_correction(claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Holm-Bonferroni across testable claims. Testing many claims makes a
    chance 'win' likely; a proven edge must survive the correction."""
    testable = sorted(
        (c for c in claims if c.get("events", 0) >= MIN_CLAIM_EVENTS),
        key=lambda c: c.get("p_value_one_sided", 1.0),
    )
    m = len(testable)
    running = 0.0
    for rank, claim in enumerate(testable):
        running = max(running, min(1.0, (m - rank) * float(claim.get("p_value_one_sided", 1.0))))
        claim["p_value_adjusted_holm"] = round(running, 4)
        if claim.get("verdict") == EDGE_PROVEN and running >= SIGNIFICANCE:
            claim["verdict"] = NO_EDGE
            claim["verdict_note"] = "not significant after correcting for the number of claims tested"
    for claim in claims:
        claim.setdefault("p_value_adjusted_holm", None)
    return claims


# ------------------------------------------------------------------------ report
def build_learning_report(
    outcomes: Iterable[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
    model: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    outcome_list = list(outcomes or [])
    rows, exclusions = validate_outcomes(outcome_list)
    direction = apply_holm_correction(evaluate_direction(rows))
    gap = evaluate_gap_size(rows)
    direction_proven = [c["claim"] for c in direction if c["verdict"] == EDGE_PROVEN]
    gap_proven = [c["claim"] for c in gap if c["verdict"] == EDGE_PROVEN]
    if direction_proven or gap_proven:
        headline = "Out-of-sample edge proven for: " + ", ".join(direction_proven + gap_proven) + "."
    else:
        headline = (
            "No prediction currently beats its naive baseline out of sample. "
            "Treat direction and gap-risk calls as research context, not a trading edge."
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": (now or datetime.now(timezone.utc)).isoformat(),
        "method": {
            "evaluation": (
                "monthly walk-forward: each month is scored with rules and models "
                "fitted only on earlier settled events"
            ),
            "min_train_events": MIN_TRAIN_EVENTS,
            "min_claim_events": MIN_CLAIM_EVENTS,
            "direction_test": "one-sided exact sign test on events where claim and baseline disagree",
            "gap_test": "bootstrap 95% CI of Brier-score improvement over the prior base rate",
            "significance": SIGNIFICANCE,
            "multiple_testing": "Holm-Bonferroni across direction claims with enough events",
            "not_measured": "trade P&L, costs and execution; gap direction is not trade profit",
        },
        "dataset": {
            "settled_outcomes": len(outcome_list),
            "validated_events": len(rows),
            "excluded": exclusions,
            "first_event_utc": rows[0]["_event_dt"].isoformat() if rows else None,
            "last_event_utc": rows[-1]["_event_dt"].isoformat() if rows else None,
        },
        "direction_claims": direction,
        "gap_size_claims": gap,
        "direction_edge_proven": bool(direction_proven),
        "gap_edge_proven": bool(gap_proven),
        "proven_claims": direction_proven + gap_proven,
        "headline": headline,
        "shadow_model": (
            {
                "version": model.get("version"),
                "trained_at_utc": model.get("trained_at_utc"),
                "training_events": model.get("training_events"),
            }
            if isinstance(model, Mapping) else None
        ),
    }
