"""§54 ML Engine — simple baseline first. A stdlib logistic baseline trained
on the existing context_builder feature surface + SignalManager closed-signal
outcomes. No sklearn, no qlib (§118). Records itself in the registry."""
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Sequence, Tuple

from core.signal_engine import SignalHorizon
from .registry import ModelRecord, TargetSpec, OosVerdictLabel


@dataclass(frozen=True)
class TrainedBaseline:
    weights: Tuple[float, ...]  # index 0 = bias, then one per feature
    feature_names: Tuple[str, ...]


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    return math.exp(z) / (1.0 + math.exp(z))


class SimpleBaselineModel:
    """§54 'simple baseline models come first'. Stdlib logistic regression via
    gradient descent. Features are the existing context_builder keys."""
    def __init__(self, *, learning_rate: float = 0.1, epochs: int = 300):
        self.learning_rate = learning_rate
        self.epochs = epochs

    def fit(self, X: Sequence[Sequence[float]], y: Sequence[float]) -> TrainedBaseline:
        if len(X) == 0 or len(y) == 0:
            raise ValueError("fit requires non-empty X and y")
        if len(X) != len(y):
            raise ValueError("X and y length mismatch")
        n_features = len(X[0])
        w = [0.0] * (n_features + 1)  # index 0 = bias
        for _ in range(self.epochs):
            grads = [0.0] * len(w)
            for xi, yi in zip(X, y):
                z = w[0] + sum(w[j + 1] * xi[j] for j in range(n_features))
                p = _sigmoid(z)
                err = p - float(yi)
                grads[0] += err
                for j in range(n_features):
                    grads[j + 1] += err * xi[j]
            m = len(X)
            for k in range(len(w)):
                w[k] -= self.learning_rate * grads[k] / m
        return TrainedBaseline(weights=tuple(w), feature_names=tuple())

    def predict_proba(self, baseline: TrainedBaseline, x: Sequence[float]) -> float:
        n = min(len(x), len(baseline.weights) - 1)
        z = baseline.weights[0] + sum(baseline.weights[j + 1] * x[j] for j in range(n))
        return _sigmoid(z)


def train_baseline(features: Sequence, outcomes: Sequence[float], *,
                   feature_names: Sequence[str], horizon: SignalHorizon,
                   target_condition: str, training_range: Tuple[str, str],
                   validation_range: Tuple[str, str], test_range: Tuple[str, str],
                   trained_at: str, model_name: str = "simple_logistic",
                   version: str = "v1", feature_version: str = "v1") -> Tuple[TrainedBaseline, ModelRecord]:
    """Train a simple baseline and produce a ModelRecord (caller registers it)."""
    if len(features) != len(outcomes):
        raise ValueError("features/outcomes length mismatch")
    X = [[float(f.get(name, 0.0)) for name in feature_names] for f in features]
    model = SimpleBaselineModel()
    trained = model.fit(X, list(outcomes))
    record = ModelRecord(
        model=model_name, version=version, features=tuple(feature_names),
        feature_version=feature_version,
        target=TargetSpec(horizon=horizon, condition=target_condition),
        horizon=horizon, training_range=tuple(training_range),
        validation_range=tuple(validation_range), test_range=tuple(test_range),
        trained_at=trained_at, is_simple_baseline=True,
        oos_verdict=OosVerdictLabel.PENDING.value,
    )
    return trained, record