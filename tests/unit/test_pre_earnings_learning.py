"""Evidence lab: walk-forward verdicts must be honest in both directions."""
import random
from datetime import datetime, timedelta, timezone

import pytest

from core import pre_earnings_learning as lab


def _rows(n, *, edge=False, seed=7, start=datetime(2026, 1, 5, tzinfo=timezone.utc)):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        event = start + timedelta(hours=6 * i)
        bias = rng.choice([-60, -30, 30, 60])
        if edge:
            up = rng.random() < (0.8 if bias > 0 else 0.2)
        else:
            up = rng.random() < 0.55
        move = rng.uniform(0.5, 9.0) * (1 if up else -1)
        gap_risk = rng.randint(5, 95)
        big = rng.random() < (gap_risk / 100 if edge else 0.2)
        out.append({
            "prediction_key": f"S{i}|{event.isoformat()}",
            "symbol": f"S{i}",
            "event_utc": event.isoformat(),
            "evaluation_utc": (event + timedelta(hours=36)).isoformat(),
            "session": rng.choice(["BMO", "AMC"]),
            "cap_bucket": "MID",
            "predicted_direction_bias": bias,
            "predicted_gap_risk": gap_risk,
            "predicted_prob_high_gap": gap_risk / 100,
            "contributors": {"news_intensity": 0.5},
            "realized_signed_move_pct": move,
            "realized_gap_move_pct": abs(move),
            "realized_high_gap": big,
        })
    return out


def _claim(claims, name):
    return next(c for c in claims if c["claim"] == name)


@pytest.mark.unit
def test_no_signal_is_reported_as_no_edge():
    report = lab.build_learning_report(_rows(1600, edge=False))
    assert report["direction_edge_proven"] is False
    assert _claim(report["direction_claims"], "heuristic_score")["verdict"] == lab.NO_EDGE
    assert "No prediction currently beats" in report["headline"]


@pytest.mark.unit
def test_real_signal_is_detected_walk_forward():
    report = lab.build_learning_report(_rows(1600, edge=True))
    heuristic = _claim(report["direction_claims"], "heuristic_score")
    assert heuristic["verdict"] == lab.EDGE_PROVEN
    assert heuristic["edge_pct_points"] > 10
    assert _claim(report["direction_claims"], "learned_model_walk_forward")["verdict"] == lab.EDGE_PROVEN
    assert _claim(report["gap_size_claims"], "stored_gap_probability")["verdict"] == lab.EDGE_PROVEN
    assert report["direction_edge_proven"] is True


@pytest.mark.unit
def test_long_leaning_claim_without_information_is_not_an_edge():
    # Up-drifting market whose monthly majority flips. The claim says LONG 85%
    # of the time at random: it beats a flipping prior-majority baseline but
    # not always-LONG, so it must not be reported as a proven edge.
    rng = random.Random(3)
    rows = _rows(2400, edge=False, seed=5)
    for i, row in enumerate(rows):
        month = datetime.fromisoformat(row["event_utc"]).month
        up_rate = 0.62 if month % 2 else 0.47
        up = rng.random() < up_rate
        row["realized_signed_move_pct"] = abs(row["realized_signed_move_pct"]) * (1 if up else -1)
        row["predicted_direction_bias"] = 40 if rng.random() < 0.85 else -40
    claim = _claim(lab.build_learning_report(rows)["direction_claims"], "heuristic_score")
    assert claim["verdict"] == lab.NO_EDGE
    assert set(claim["baselines"]) == {"prior_majority", "always_long"}


@pytest.mark.unit
def test_validation_excludes_untrustworthy_targets_and_counts_reasons():
    rows = _rows(4)
    rows[0]["session"] = "UNKNOWN"
    rows[1]["realized_signed_move_pct"] = 859.4
    rows[2]["evaluation_utc"] = "2020-01-01T00:00:00+00:00"
    rows.append(dict(rows[3]))
    kept, excluded = lab.validate_outcomes(rows)
    assert len(kept) == 1
    assert excluded == {
        "session_unknown": 1,
        "implausible_move_needs_manual_review": 1,
        "settled_before_event": 1,
        "duplicate_event_kept_latest": 1,
    }


@pytest.mark.unit
def test_walk_forward_never_trains_on_the_test_month(monkeypatch):
    seen = []
    real_fit = lab.fit_logistic

    def spy(X, y, **kw):
        seen.append(len(y))
        return real_fit(X, y, **kw)

    monkeypatch.setattr(lab, "fit_logistic", spy)
    rows, _ = lab.validate_outcomes(_rows(1600))
    folds = lab._monthly_folds(rows, lab.MIN_TRAIN_EVENTS)
    lab.evaluate_direction(rows)
    assert len(seen) == len(folds) > 0
    for (month, train, _test), n_train in zip(folds, seen):
        assert all(r["_month"] < month for r in train)
        assert n_train == len(train)


@pytest.mark.unit
def test_small_samples_are_insufficient_not_proven():
    report = lab.build_learning_report(_rows(80, edge=True))
    assert all(c["verdict"] == lab.INSUFFICIENT_EVIDENCE for c in report["direction_claims"])
    assert report["direction_edge_proven"] is False


@pytest.mark.unit
def test_shadow_model_round_trip_and_live_shadow_scoring():
    outcomes = _rows(1600, edge=True)
    model = lab.fit_learning_model(outcomes, trained_at="2026-09-15T00:00:00+00:00")
    assert model and model["training_events"] == 1600
    fields = lab.shadow_predictions(model, {
        "predicted_direction_bias": 60, "session": "AMC",
        "predicted_gap_risk": 80, "predicted_prob_high_gap": 0.8,
    })
    assert set(fields) == set(lab.SHADOW_FIELDS)
    assert fields["shadow_direction_prob_up"] > 0.6
    settled = _rows(150, edge=True, seed=11, start=datetime(2026, 9, 1, tzinfo=timezone.utc))
    for row in settled:
        row.update(lab.shadow_predictions(model, row))
    report = lab.build_learning_report(settled, model=model)
    live = _claim(report["direction_claims"], "learned_model_live_shadow")
    assert live["events"] == 150 and live["accuracy_pct"] > 70
    assert report["shadow_model"]["version"] == model["version"]


@pytest.mark.unit
def test_shadow_predictions_without_model_are_empty():
    assert lab.shadow_predictions(None, {"predicted_direction_bias": 10}) == {}
    assert lab.fit_learning_model(_rows(50)) is None


@pytest.mark.unit
def test_report_is_json_serializable():
    import json

    report = lab.build_learning_report(_rows(900))
    json.dumps(report, allow_nan=False)


@pytest.mark.unit
def test_statistics_helpers():
    assert lab.binomial_upper_tail(0, 0) == 1.0
    assert abs(lab.binomial_upper_tail(5, 10) - 0.623046875) < 1e-9
    assert lab.roc_auc([0.1, 0.1, 0.9, 0.9], [0, 1, 0, 1]) == 0.5
    assert lab.roc_auc([0.1, 0.9], [0, 1]) == 1.0
    assert lab.wilson_interval(0, 0) is None
    mapping = lab.fit_isotonic([0.1, 0.2, 0.3, 0.4], [0, 1, 0, 1])
    assert lab.apply_isotonic(mapping, 0.35) == pytest.approx(0.5)
