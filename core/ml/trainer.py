"""ML Model trainer — trains the SimpleBaseline model on existing signal data
and registers it in config.json ml.registry + MLflow.

Called automatically at startup when ml.registry is empty (or
FORCE_ML_TRAIN=1 is set). Uses only stdlib + the existing V9 signal store —
no sklearn, no external deps beyond what is already required.

MLflow is used when available (MLFLOW_TRACKING_URI configured); the model
is persisted and registered even without MLflow.
"""
from __future__ import annotations

import json
import logging
import os
import pickle
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_TRAIN_LOCK = threading.Lock()


# ── config helpers ─────────────────────────────────────────────────────────────

def _models_dir() -> Path:
    from core.config import DATA_DIR
    d = Path(DATA_DIR) / "ml_models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _registry_from_config(config: Any) -> List[Dict[str, Any]]:
    root = getattr(config, "config", config) if config is not None else {}
    if not isinstance(root, dict):
        try:
            from core.config import CONFIG_PATH, ConfigManager

            root = ConfigManager(path=CONFIG_PATH).config
        except Exception:
            root = {}
    ml = (root or {}).get("ml") or {}
    registry = ml.get("registry") or []
    return list(registry) if isinstance(registry, list) else []


def _write_registry_to_config(config: Any, registry: List[Dict[str, Any]]) -> None:
    """Persist ``ml.registry`` through the canonical runtime ConfigManager."""
    try:
        from core.config import CONFIG_PATH, ConfigManager

        manager = config if callable(getattr(config, "update_value", None)) else ConfigManager(path=CONFIG_PATH)
        manager.update_value(["ml", "registry"], list(registry))

        # Keep the API's long-lived config object coherent when training was
        # invoked without passing that object directly.
        try:
            from api.state_service import get_shared_config
            cfg = get_shared_config()
            if cfg is not manager and callable(getattr(cfg, "update_value", None)):
                cfg.update_value(["ml", "registry"], list(registry))
        except Exception:
            pass
    except Exception:
        logger.exception("Failed to persist ml.registry to canonical runtime config")


# ── signal data loader ─────────────────────────────────────────────────────────

def _load_signal_samples() -> Tuple[List[Dict[str, Any]], List[float]]:
    """Load closed-trade signal features and binary outcomes from runtime data.

    Feature vector per trade:
      mtf_alignment, macd_score, rsi_score, vwap_score, ema_score,
      atr_pct, iv_rv_ratio (0 when absent)

    Target: 1.0 if P&L > 0, else 0.0
    """
    features: List[Dict[str, Any]] = []
    outcomes: List[float] = []

    def _extract_feature(entry: Dict[str, Any]) -> Dict[str, float]:
        score = float(entry.get("score", 70.0) or 70.0)
        return {
            "mtf_alignment": float(entry.get("mtf_alignment", entry.get("mtf_score", score / 20.0))),
            "macd_score": float(entry.get("macd_score", 0.5)),
            "rsi_score": float(entry.get("rsi_score", 0.5)),
            "vwap_score": float(entry.get("vwap_score", 0.5)),
            "ema_score": float(entry.get("ema_score", 0.5)),
            "atr_pct": float(entry.get("atr_pct", 0.02)),
            "iv_rv_ratio": float(entry.get("iv_rv_ratio", 1.0) or 1.0),
        }

    # Primary source: runtime trade journal / capital ledger
    from core.config import DATA_DIR, RUNTIME_DIR, SIGNALS_PATH

    candidate_paths = [
        Path(RUNTIME_DIR) / "trade_journal.json",
        Path(DATA_DIR) / "trade_journal.json",
    ]
    for path in candidate_paths:
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            trades = raw if isinstance(raw, list) else (raw.get("trades") or [])
            for trade in trades:
                if not isinstance(trade, dict):
                    continue
                pnl = trade.get("realized_pnl") or trade.get("pnl") or 0.0
                entry = trade.get("entry_signal") or trade.get("signal") or {}
                feat = _extract_feature(entry)
                features.append(feat)
                outcomes.append(1.0 if float(pnl) > 0 else 0.0)
        except Exception:
            logger.debug("Could not load trade journal from %s", path, exc_info=True)

    # Secondary source: SQLite Capital state closed positions
    try:
        from core.sqlite_store import get_sqlite_store
        store = get_sqlite_store()
        cap = store.get_json("capital_state")
        if isinstance(cap, dict):
            positions = cap.get("positions", [])
            for pos in positions:
                if not isinstance(pos, dict):
                    continue
                if str(pos.get("status", "")).upper() == "CLOSED":
                    pnl = float(pos.get("realized_pnl", pos.get("net_pnl", 0.0)) or 0.0)
                    exp = pos.get("explainability", {}) if isinstance(pos.get("explainability"), dict) else {}
                    feat = _extract_feature(exp)
                    features.append(feat)
                    outcomes.append(1.0 if pnl > 0 else 0.0)
    except Exception:
        pass

    # Tertiary source: signals_state.summary.json (closed signals)
    if len(features) < 10:
        summary_path = Path(SIGNALS_PATH).with_name(f"{Path(SIGNALS_PATH).stem}.summary.json")
        if summary_path.exists():
            try:
                raw = json.loads(summary_path.read_text(encoding="utf-8"))
                signals = (raw if isinstance(raw, list)
                           else raw.get("closed_signals") or raw.get("signals") or [])
                for sig in signals:
                    if not isinstance(sig, dict):
                        continue
                    outcome = sig.get("outcome") or sig.get("result") or ""
                    if str(outcome).upper() not in {"WIN", "LOSS", "CLOSED"}:
                        continue
                    pnl = sig.get("realized_pnl") or sig.get("pnl") or 0.0
                    feat = _extract_feature(sig)
                    features.append(feat)
                    outcomes.append(1.0 if float(pnl) > 0 else 0.0)
            except Exception:
                logger.debug("Could not parse signals summary", exc_info=True)

    return features, outcomes


# ── MLflow logging ─────────────────────────────────────────────────────────────

def _log_to_mlflow(run_name: str, params: Dict[str, Any],
                   metrics: Dict[str, Any], model_path: Path) -> Optional[str]:
    """Log training run to MLflow. Returns run_id or None."""
    uri = os.environ.get("MLFLOW_TRACKING_URI", "").strip()
    if not uri:
        return None
    try:
        import mlflow
        mlflow.set_tracking_uri(uri)
        mlflow.set_experiment("market_signals_v9_ml")
        with mlflow.start_run(run_name=run_name) as run:
            mlflow.log_params({k: str(v) for k, v in params.items()})
            mlflow.log_metrics({k: float(v) for k, v in metrics.items()
                                if isinstance(v, (int, float))})
            mlflow.log_artifact(str(model_path), artifact_path="model")
            run_id = run.info.run_id
        logger.info("MLflow run logged: %s", run_id)
        return run_id
    except Exception as exc:
        logger.warning("MLflow logging skipped: %s", exc)
        return None


# ── public trainer ─────────────────────────────────────────────────────────────

MIN_SAMPLES = 10

FEATURE_NAMES = [
    "mtf_alignment", "macd_score", "rsi_score",
    "vwap_score", "ema_score", "atr_pct", "iv_rv_ratio",
]


def train_and_register_baseline(config: Any = None, *,
                                 force: bool = False) -> Dict[str, Any]:
    """Train SimpleBaselineModel, compute calibration + meta-labeling, and register in config + MLflow.

    Returns a result dict: status, model_id, samples, accuracy, detail.
    """
    with _TRAIN_LOCK:
        return _do_train(config, force=force)


def _do_train(config: Any, *, force: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "status": "SKIPPED", "model_id": "", "samples": 0,
        "accuracy": None, "mlflow_run_id": None, "detail": "",
        "calibration": None, "meta_labeling": None,
    }

    # Skip if registry already has records (unless forced)
    existing = _registry_from_config(config)
    if existing and not force:
        result["detail"] = f"ml.registry already has {len(existing)} record(s); skipping"
        result["status"] = "ALREADY_REGISTERED"
        return result

    # Load data
    features, outcomes = _load_signal_samples()
    result["samples"] = len(features)

    if len(features) < MIN_SAMPLES:
        result["status"] = "INSUFFICIENT_DATA"
        result["detail"] = (
            f"Only {len(features)} closed-trade samples found "
            f"(minimum {MIN_SAMPLES} required). "
            "The model will be trained automatically once more trades close."
        )
        logger.info("ML baseline: %s", result["detail"])
        return result

    # Split: 70% train, 15% val, 15% test
    n = len(features)
    train_end = max(1, int(n * 0.70))
    val_end = max(train_end + 1, int(n * 0.85)) if n > train_end + 1 else n
    X_train = features[:train_end]
    y_train = outcomes[:train_end]
    X_val = features[train_end:val_end] if val_end > train_end else X_train
    y_val = outcomes[train_end:val_end] if val_end > train_end else y_train
    X_test = features[val_end:] if n > val_end else X_val
    y_test = outcomes[val_end:] if n > val_end else y_val

    now_str = datetime.now(timezone.utc).isoformat()
    date_str = now_str[:10]

    # Train Baseline Model
    from core.ml.baseline import SimpleBaselineModel, TrainedBaseline
    model = SimpleBaselineModel(learning_rate=0.05, epochs=500)
    X_train_lists = [[f.get(name, 0.0) for name in FEATURE_NAMES] for f in X_train]
    trained: TrainedBaseline = model.fit(X_train_lists, y_train)

    # Evaluate on test set
    def _accuracy(X_eval: List[Dict], y_eval: List[float]) -> float:
        if not X_eval:
            return 0.0
        correct = sum(
            1 for feat, label in zip(X_eval, y_eval)
            if (model.predict_proba(trained, [feat.get(n, 0.0) for n in FEATURE_NAMES]) >= 0.5)
            == (label >= 0.5)
        )
        return correct / len(X_eval)

    acc_train = _accuracy(X_train, y_train)
    acc_val = _accuracy(X_val, y_val) if X_val else acc_train
    acc_test = _accuracy(X_test, y_test) if X_test else acc_val

    # 1. Calibration (Isotonic PAV)
    calibration_info: Dict[str, Any] = {}
    try:
        from core.ml.calibration import ProbabilityDefinition, calibrate
        from core.signal_engine import SignalHorizon
        val_probs = [
            model.predict_proba(trained, [f.get(name, 0.0) for name in FEATURE_NAMES])
            for f in X_val
        ]
        cal_res = calibrate(
            val_probs, y_val,
            definition=ProbabilityDefinition(horizon=SignalHorizon.SHORT_TERM, condition="realized_pnl > 0"),
            method="isotonic"
        )
        calibration_info = {
            "method": cal_res.method,
            "sample_size": cal_res.sample_size,
            "is_statistical_probability": cal_res.is_statistical_probability,
            "mapping_points": len(cal_res.mapping),
        }
    except Exception as exc:
        logger.warning("ML calibration step warning: %s", exc)

    # 2. Meta-Labeling Evaluation
    meta_info: Dict[str, Any] = {}
    try:
        from core.ml.meta_label import evaluate_meta_labeling
        test_probs = [
            model.predict_proba(trained, [f.get(name, 0.0) for name in FEATURE_NAMES])
            for f in X_test
        ]
        primary_preds = ["LONG" if p >= 0.5 else "SHORT" for p in test_probs]
        meta_preds = ["EXECUTE" if p >= 0.55 or p <= 0.45 else "DO_NOT_EXECUTE" for p in test_probs]
        realized_labels = ["LONG" if y >= 0.5 else "SHORT" for y in y_test]
        meta_eval = evaluate_meta_labeling(primary_preds, meta_preds, realized_labels, positive_label="LONG")
        meta_info = {
            "primary_precision": meta_eval.primary_precision,
            "meta_precision": meta_eval.meta_precision,
            "meta_labeling_improves_precision": meta_eval.meta_labeling_improves_precision,
            "sample_size": meta_eval.sample_size,
        }
    except Exception as exc:
        logger.warning("ML meta-labeling step warning: %s", exc)

    # Persist model artifact
    models_dir = _models_dir()
    model_version = f"v1_{date_str.replace('-', '')}"
    model_file = models_dir / f"simple_logistic_{model_version}.pkl"
    try:
        with open(model_file, "wb") as f:
            pickle.dump({
                "model": model,
                "trained": trained,
                "feature_names": FEATURE_NAMES,
                "calibration": calibration_info,
                "meta_labeling": meta_info,
                "trained_at": now_str,
            }, f)
    except Exception as exc:
        result["status"] = "ERROR"
        result["detail"] = f"Model artifact save failed: {exc}"
        logger.exception("ML baseline model save failed")
        return result

    # Log to MLflow
    params = {
        "model": "simple_logistic", "version": model_version,
        "feature_version": "v1", "n_features": len(FEATURE_NAMES),
        "learning_rate": 0.05, "epochs": 500,
        "n_samples": n, "n_train": len(X_train),
        "n_val": len(X_val), "n_test": len(X_test),
    }
    metrics = {
        "accuracy_train": acc_train,
        "accuracy_val": acc_val,
        "accuracy_test": acc_test,
    }
    if meta_info:
        metrics["meta_precision"] = meta_info.get("meta_precision", 0.0)
    mlflow_run_id = _log_to_mlflow(
        run_name=f"simple_logistic_{model_version}",
        params=params, metrics=metrics, model_path=model_file,
    )

    # Build ModelRecord dict
    record = {
        "model": "simple_logistic",
        "version": model_version,
        "features": FEATURE_NAMES,
        "feature_version": "v1",
        "target": {"horizon": "SHORT_TERM", "condition": "realized_pnl > 0"},
        "horizon": "SHORT_TERM",
        "training_range": [date_str, date_str],
        "validation_range": [date_str, date_str],
        "test_range": [date_str, date_str],
        "trained_at": now_str,
        "is_simple_baseline": True,
        "oos_verdict": "PENDING",
        "artifact_path": str(model_file),
        "accuracy_train": round(acc_train, 4),
        "accuracy_val": round(acc_val, 4),
        "accuracy_test": round(acc_test, 4),
        "n_samples": n,
        "calibration": calibration_info,
        "meta_labeling": meta_info,
        "mlflow_run_id": mlflow_run_id,
    }

    # Persist to config.json
    registry = [record] if force else (list(existing) + [record])
    _write_registry_to_config(config, registry)

    result.update(
        status="TRAINED",
        model_id=f"simple_logistic:{model_version}",
        samples=n,
        accuracy=round(acc_test, 4),
        calibration=calibration_info,
        meta_labeling=meta_info,
        mlflow_run_id=mlflow_run_id,
        detail=(
            f"Baseline trained on {n} samples — "
            f"acc_train={acc_train:.3f} val={acc_val:.3f} test={acc_test:.3f}. "
            f"Artifact: {model_file}"
            + (f" | MLflow run: {mlflow_run_id}" if mlflow_run_id else "")
        ),
    )
    logger.info("ML baseline: %s", result["detail"])
    return result


def check_and_train_baseline(config: Any = None) -> Dict[str, Any]:
    """Check whether training is needed and run if so (non-blocking intent)."""
    force = os.environ.get("FORCE_ML_TRAIN", "").strip().lower() in {"1", "true", "yes"}
    return train_and_register_baseline(config, force=force)


def get_ml_status(config: Any = None) -> Dict[str, Any]:
    """Get current status of ML models, training readiness, and registry."""
    records = _registry_from_config(config)
    verified_records = []
    for r in records:
        path = str(r.get("artifact_path") or "").strip()
        exists = bool(path and Path(path).exists())
        verified_records.append({**r, "artifact_exists": exists})

    features, outcomes = _load_signal_samples()
    samples_count = len(features)
    has_active = any(r.get("artifact_exists") for r in verified_records) or (len(records) > 0)

    return {
        "status": "HEALTHY" if has_active else ("INSUFFICIENT_DATA" if samples_count < MIN_SAMPLES else "READY_TO_TRAIN"),
        "active_models_count": len([r for r in verified_records if r.get("artifact_exists")]),
        "total_registered": len(records),
        "available_samples": samples_count,
        "min_samples_required": MIN_SAMPLES,
        "ready_to_train": samples_count >= MIN_SAMPLES,
        "registry": verified_records,
        "models_dir": str(_models_dir()),
    }


__all__ = ["train_and_register_baseline", "check_and_train_baseline", "get_ml_status"]
