"""Agent captures free pre-event evidence; the lab corrects for many claims."""
import importlib.util
import pathlib
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agents import pre_earnings_agent as pe
from core import estimate_revisions, pre_event_features
from core import pre_earnings_learning as lab

_HERE = pathlib.Path(__file__).parent


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_capture_helpers_{name}", _HERE / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_dossier = _load("test_pre_earnings_verification_dossier.py")
_synthetic = _load("test_pre_earnings_learning.py")
BEFORE = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
AFTER = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
SNAPSHOT = {"predicted_eps_trend_change_30d_pct": 4.2, "predicted_eps_revisions_up_30d": 3}
PRICES = {"predicted_rel_return_5d_pct": -11.5, "predicted_rel_return_20d_pct": -3.0,
          "predicted_price_features_as_of": "2026-09-04"}


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "PRE_EARNINGS_FEEDBACK_PATH", tmp_path / "pre_earnings_feedback.json")
    return _dossier._serving_agent()


def _register(agent, item, now):
    with patch.object(pe, "_utc_now", lambda: now):
        agent._register_open_predictions([item], agent._get_config())
    return next(iter(agent._feedback["open_predictions"].values()))


@pytest.mark.unit
def test_registration_copies_cached_pre_event_evidence(agent):
    revision_cache, price_cache = agent._pre_event_feature_caches()
    revision_cache.put("TEST", dict(SNAPSHOT))
    price_cache["TEST"] = {"ts": BEFORE.timestamp(), "features": dict(PRICES)}
    record = _register(agent, _dossier._long_item(), BEFORE)
    assert record["predicted_eps_trend_change_30d_pct"] == 4.2
    assert record["predicted_rel_return_5d_pct"] == -11.5


@pytest.mark.unit
def test_evidence_is_frozen_after_event_start(agent):
    revision_cache, price_cache = agent._pre_event_feature_caches()
    revision_cache.put("TEST", dict(SNAPSHOT))
    price_cache["TEST"] = {"ts": BEFORE.timestamp(), "features": dict(PRICES)}
    _register(agent, _dossier._long_item(), BEFORE)
    revision_cache.put("TEST", {"predicted_eps_trend_change_30d_pct": -9.0})
    price_cache["TEST"] = {"ts": AFTER.timestamp(), "features": {"predicted_rel_return_5d_pct": 30.0}}
    record = _register(agent, _dossier._long_item(), AFTER)
    assert record["predicted_eps_trend_change_30d_pct"] == 4.2
    assert record["predicted_rel_return_5d_pct"] == -11.5


@pytest.mark.unit
def test_refresh_is_disabled_under_tests(agent):
    assert agent._refresh_pre_event_features([_dossier._long_item()], agent._get_config(), now=BEFORE) == 0


@pytest.mark.unit
def test_refresh_fetches_nearest_upcoming_events_within_budget(agent, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("TEST_MODE", raising=False)
    fetched = []
    monkeypatch.setattr(estimate_revisions, "fetch_snapshot", lambda s: fetched.append(s) or dict(SNAPSHOT))
    monkeypatch.setattr(pre_event_features, "relative_returns", lambda bars, bench, now: dict(PRICES))
    monkeypatch.setattr(agent, "_load_symbol_daily_bars", lambda symbol: [])
    items = [
        {"symbol": "FAR", "earnings_time_utc": (BEFORE + timedelta(days=6)).isoformat()},
        {"symbol": "NEAR", "earnings_time_utc": (BEFORE + timedelta(days=1)).isoformat()},
        {"symbol": "PAST", "earnings_time_utc": (BEFORE - timedelta(days=1)).isoformat()},
        {"symbol": "LATER", "earnings_time_utc": (BEFORE + timedelta(days=30)).isoformat()},
    ]
    cfg = dict(agent._get_config(), pre_event_feature_max_fetch_per_cycle=1)
    assert agent._refresh_pre_event_features(items, cfg, now=BEFORE) == 2
    assert fetched == ["NEAR"]
    revision_cache, price_cache = agent._pre_event_feature_caches()
    assert revision_cache.get("NEAR", 3600) == SNAPSHOT
    assert price_cache["NEAR"]["features"] == PRICES
    cfg["estimate_revision_capture_enabled"] = False
    assert agent._refresh_pre_event_features(items, cfg, now=BEFORE) == 0


@pytest.mark.unit
def test_holm_correction_downgrades_chance_wins():
    claims = [
        {"claim": "a", "events": 500, "p_value_one_sided": 0.03, "verdict": lab.EDGE_PROVEN},
        {"claim": "b", "events": 500, "p_value_one_sided": 0.40, "verdict": lab.NO_EDGE},
        {"claim": "c", "events": 500, "p_value_one_sided": 0.001, "verdict": lab.EDGE_PROVEN},
        {"claim": "d", "events": 20, "p_value_one_sided": 0.01, "verdict": lab.INSUFFICIENT_EVIDENCE},
    ]
    out = {c["claim"]: c for c in lab.apply_holm_correction(claims)}
    assert out["c"]["verdict"] == lab.EDGE_PROVEN and out["c"]["p_value_adjusted_holm"] == 0.003
    assert out["a"]["verdict"] == lab.NO_EDGE and out["a"]["p_value_adjusted_holm"] == 0.06
    assert out["d"]["p_value_adjusted_holm"] is None


@pytest.mark.unit
def test_reversal_claim_only_scores_events_after_preregistration():
    registered = datetime.fromisoformat(pre_event_features.REVERSAL_HYPOTHESIS_REGISTERED_UTC)
    rows = _synthetic._rows(1600)  # spans 2026-01 .. 2027-02
    for row in rows:
        if datetime.fromisoformat(row["event_utc"]) < registered:
            row["predicted_rel_return_5d_pct"] = -20.0
    claims = {c["claim"]: c for c in lab.build_learning_report(rows)["direction_claims"]}
    # The exploratory period that suggested the rule can never count as evidence.
    assert claims["pre_event_reversal_5d"]["events"] == 0
    assert "p_value_adjusted_holm" in claims["heuristic_score"]

    for row in rows:
        row["predicted_rel_return_5d_pct"] = -20.0
    claims = {c["claim"]: c for c in lab.build_learning_report(rows)["direction_claims"]}
    forward = [r for r in rows if datetime.fromisoformat(r["event_utc"]) >= registered
               and r["realized_signed_move_pct"] != 0]
    assert claims["pre_event_reversal_5d"]["events"] == len(forward) > 0
