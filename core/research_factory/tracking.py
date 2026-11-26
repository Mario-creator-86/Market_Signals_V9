"""Phase 14 — research tracking adapter seam (spec §17 boundary).

V9 keeps its own `ResearchTracking` interface; the default implementation is
a deterministic JSON collector that lands in the experiment evidence. An
optional MLflow adapter activates only when `MLFLOW_TRACKING_URI` is set AND
mlflow imports — domain code never imports MLflow directly and V9 remains
fully functional without it.
"""
from __future__ import annotations

import os
import sys
import json
import math
from numbers import Real
from typing import Any, Dict, Optional, Protocol


def _ensure_utf8_standard_streams() -> None:
    """Make MLflow's Unicode run links safe on Windows cp1252 consoles."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        encoding = str(getattr(stream, "encoding", "") or "").lower().replace("_", "-")
        if encoding in {"utf-8", "utf8"}:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Captured/closed streams are allowed; tracking still falls back
            # honestly if MLflow itself cannot initialize.
            continue


class ResearchTracking(Protocol):
    def start_run(self, experiment_id: str, run_name: str) -> str: ...
    def log_params(self, run_id: str, params: Dict[str, Any]) -> None: ...
    def log_metrics(self, run_id: str, metrics: Dict[str, Any]) -> None: ...
    def finish(self, run_id: str, status: str) -> None: ...


class JsonTracking:
    """Default — collects into a dict the caller persists as evidence."""

    def __init__(self) -> None:
        self._runs: Dict[str, Dict[str, Any]] = {}

    def start_run(self, experiment_id: str, run_name: str) -> str:
        run_id = f"{experiment_id}:{run_name}"
        self._runs[run_id] = {"experiment_id": experiment_id, "run_name": run_name,
                              "params": {}, "metrics": {}, "status": "RUNNING"}
        return run_id

    def log_params(self, run_id: str, params: Dict[str, Any]) -> None:
        self._runs.setdefault(run_id, {})["params"] = dict(params)

    def log_metrics(self, run_id: str, metrics: Dict[str, Any]) -> None:
        run = self._runs.setdefault(run_id, {"params": {}, "metrics": {}})
        run.setdefault("metrics", {}).update(dict(metrics))

    def finish(self, run_id: str, status: str) -> None:
        self._runs.setdefault(run_id, {})["status"] = str(status)

    def evidence(self) -> Dict[str, Any]:
        return {run_id: dict(run) for run_id, run in self._runs.items()}


class MlflowTracking:
    """Optional adapter — lazy import, enabled by MLFLOW_TRACKING_URI."""

    def __init__(self, tracking_uri: Optional[str] = None):
        _ensure_utf8_standard_streams()
        import mlflow  # lazy: only when explicitly enabled

        self._mlflow = mlflow
        uri = tracking_uri or os.environ.get("MLFLOW_TRACKING_URI", "")
        if uri:
            mlflow.set_tracking_uri(uri)

    def start_run(self, experiment_id: str, run_name: str) -> str:
        with self._mlflow.start_run(run_name=run_name, tags={"experiment_id": experiment_id}) as run:
            return run.info.run_id

    def log_params(self, run_id: str, params: Dict[str, Any]) -> None:
        with self._mlflow.start_run(run_id=run_id):
            self._mlflow.log_params({k: str(v) for k, v in params.items()})

    def log_metrics(self, run_id: str, metrics: Dict[str, Any]) -> None:
        numeric: Dict[str, float] = {}
        descriptive: Dict[str, str] = {}
        for key, value in metrics.items():
            name = str(key)
            if isinstance(value, Real) and math.isfinite(float(value)):
                numeric[name] = float(value)
                continue
            if isinstance(value, (dict, list, tuple)):
                rendered = json.dumps(value, ensure_ascii=False, default=str)
            elif value is None:
                rendered = "null"
            else:
                rendered = str(value)
            descriptive[f"evidence.{name}"] = rendered[:5000]
        with self._mlflow.start_run(run_id=run_id):
            if numeric:
                self._mlflow.log_metrics(numeric)
            if descriptive:
                self._mlflow.set_tags(descriptive)

    def finish(self, run_id: str, status: str) -> None:
        with self._mlflow.start_run(run_id=run_id):
            self._mlflow.set_tag("status", str(status))


def resolve_tracking() -> ResearchTracking:
    """MLflow when configured and importable; JSON collector otherwise."""
    if os.environ.get("MLFLOW_TRACKING_URI", "").strip():
        try:
            return MlflowTracking()
        except Exception:
            pass
    return JsonTracking()


__all__ = ["ResearchTracking", "JsonTracking", "MlflowTracking", "resolve_tracking"]
