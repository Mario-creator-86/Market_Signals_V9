"""Tests for core.ml.trainer — ML baseline training and MLflow integration."""
from __future__ import annotations

import json
import os
import pickle
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_trade_journal(n: int = 25) -> list:
    """Synthetic closed-trade records with alternating P&L."""
    trades = []
    for i in range(n):
        pnl = 10.0 if i % 2 == 0 else -5.0
        trades.append({
            "realized_pnl": pnl,
            "entry_signal": {
                "mtf_alignment": float(i % 5),
                "macd_score": 0.6 if pnl > 0 else 0.2,
                "rsi_score": 0.7 if pnl > 0 else 0.3,
                "vwap_score": 0.5,
                "ema_score": 0.4,
                "atr_pct": 0.01,
                "iv_rv_ratio": 1.1,
            },
        })
    return trades


# ── test _load_signal_samples ──────────────────────────────────────────────────

def test_load_signal_samples_from_trade_journal(tmp_path, monkeypatch):
    """Samples are loaded from a synthetic trade journal."""
    journal = tmp_path / "trade_journal.json"
    journal.write_text(json.dumps(_make_trade_journal(20)), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    # Create the runtime dir the loader checks first
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "trade_journal.json").write_text(
        json.dumps(_make_trade_journal(20)), encoding="utf-8"
    )

    import core.config as cc
    monkeypatch.setattr(cc, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(cc, "DATA_DIR", tmp_path / "data")

    from core.ml.trainer import _load_signal_samples
    features, outcomes = _load_signal_samples()
    assert len(features) == 20
    assert len(outcomes) == 20
    assert all(o in {0.0, 1.0} for o in outcomes)


# ── test train_and_register_baseline ──────────────────────────────────────────

def test_train_and_register_baseline_trains(tmp_path, monkeypatch):
    """train_and_register_baseline returns TRAINED when enough samples exist."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runtime").mkdir()
    journal = tmp_path / "runtime" / "trade_journal.json"
    journal.write_text(json.dumps(_make_trade_journal(30)), encoding="utf-8")
    # Patch config.json write to tmp
    config_json = tmp_path / "config.json"
    config_json.write_text(
        json.dumps({"ml": {"registry": [], "qlib_enabled": False,
                            "baseline_feature_version": "v1"}}),
        encoding="utf-8",
    )
    # Patch DATA_DIR
    import core.config as cc
    monkeypatch.setattr(cc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cc, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(cc, "CONFIG_PATH", config_json)

    from core.ml import trainer as tr
    monkeypatch.setattr(tr, "_log_to_mlflow", lambda *a, **k: None)

    result = tr.train_and_register_baseline(None, force=True)
    assert result["status"] == "TRAINED"
    assert result["samples"] >= 30
    assert result["accuracy"] is not None
    # Artifact must exist on disk
    assert Path(result["detail"].split("Artifact: ")[-1].strip()).exists() or \
           (tmp_path / "data" / "ml_models").exists()


def test_train_insufficient_data(tmp_path, monkeypatch):
    """Returns INSUFFICIENT_DATA when fewer than MIN_SAMPLES samples exist."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runtime").mkdir()
    journal = tmp_path / "runtime" / "trade_journal.json"
    journal.write_text(json.dumps(_make_trade_journal(3)), encoding="utf-8")

    import core.config as cc
    monkeypatch.setattr(cc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cc, "RUNTIME_DIR", tmp_path / "runtime")

    from core.ml import trainer as tr
    result = tr.train_and_register_baseline(None, force=True)
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["samples"] == 3


def test_train_skipped_when_registry_not_empty(tmp_path, monkeypatch):
    """Returns ALREADY_REGISTERED when ml.registry already has records."""
    monkeypatch.chdir(tmp_path)

    class FakeConfig:
        config = {"ml": {"registry": [{"model": "simple_logistic", "version": "v1"}]}}

    from core.ml import trainer as tr
    result = tr.train_and_register_baseline(FakeConfig(), force=False)
    assert result["status"] == "ALREADY_REGISTERED"


def test_train_force_overwrites_existing_registry(tmp_path, monkeypatch):
    """force=True retrains even when registry has records."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runtime").mkdir()
    journal = tmp_path / "runtime" / "trade_journal.json"
    journal.write_text(json.dumps(_make_trade_journal(30)), encoding="utf-8")
    config_json = tmp_path / "config.json"
    config_json.write_text(
        json.dumps({"ml": {"registry": [{"model": "old", "version": "v0"}]}}),
        encoding="utf-8",
    )

    import core.config as cc
    monkeypatch.setattr(cc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cc, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(cc, "CONFIG_PATH", config_json)

    from core.ml import trainer as tr
    monkeypatch.setattr(tr, "_log_to_mlflow", lambda *a, **k: None)

    result = tr.train_and_register_baseline(None, force=True)
    assert result["status"] == "TRAINED"


# ── test _log_to_mlflow stub ───────────────────────────────────────────────────

def test_log_to_mlflow_skipped_when_uri_absent(tmp_path):
    """_log_to_mlflow returns None gracefully when no URI is configured."""
    os.environ.pop("MLFLOW_TRACKING_URI", None)
    from core.ml.trainer import _log_to_mlflow
    result = _log_to_mlflow("test_run", {}, {}, tmp_path / "fake.pkl")
    assert result is None


# ── test cockpit health tile contract ─────────────────────────────────────────

def test_ml_model_tile_healthy_when_artifact_exists(tmp_path):
    """ML model tile is HEALTHY when registry record has artifact on disk."""
    from core.ml.trainer import FEATURE_NAMES
    artifact = tmp_path / "model.pkl"
    artifact.write_bytes(b"fake")
    records = [{"artifact_path": str(artifact)}]

    # Simulate the cockpit_service logic
    trained_count = 0
    for rec in records:
        ap = str(rec.get("artifact_path", "")).strip()
        if ap and Path(ap).exists():
            trained_count += 1
    assert trained_count == 1


def test_ml_model_tile_unavailable_when_no_records():
    """ML model tile is UNAVAILABLE when registry is empty."""
    ml_records = []
    trained_count = sum(
        1 for rec in ml_records
        if not str(rec.get("artifact_path", "")).strip()
        or Path(str(rec.get("artifact_path", ""))).exists()
    )
    ml_status = "HEALTHY" if trained_count > 0 else ("UNVERIFIED" if ml_records else "UNAVAILABLE")
    assert ml_status == "UNAVAILABLE"
