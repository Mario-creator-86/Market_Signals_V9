"""Immutable strategy lifecycle, promotion evidence, and audit types."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported immutable value: {type(value).__name__}")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _utc_iso(value: str, *, name: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _finite(value: Any, *, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


class StrategyStatus(str, Enum):
    RESEARCH = "RESEARCH"
    CHALLENGER = "CHALLENGER"
    SHADOW = "SHADOW"
    APPROVED = "APPROVED"
    CHAMPION = "CHAMPION"
    DEGRADED = "DEGRADED"
    RETIRED = "RETIRED"


@dataclass(frozen=True)
class StrategyRecord:
    strategy_id: str
    version: str
    description: str
    features: tuple[str, ...]
    entry_rules: str
    exit_rules: str
    parameters: Mapping[str, Any]
    horizon: str
    universe: tuple[str, ...]
    created_by: str
    created_at: str
    status: StrategyStatus = StrategyStatus.RESEARCH

    def __post_init__(self) -> None:
        strategy_id = str(self.strategy_id or "").strip().upper()
        version = str(self.version or "").strip()
        description = str(self.description or "").strip()
        features = tuple(dict.fromkeys(str(item or "").strip() for item in self.features if str(item or "").strip()))
        entry_rules = str(self.entry_rules or "").strip()
        exit_rules = str(self.exit_rules or "").strip()
        horizon = str(self.horizon or "").strip().upper()
        universe = tuple(sorted({str(item or "").strip().upper() for item in self.universe if str(item or "").strip()}))
        created_by = str(self.created_by or "").strip()
        try:
            status = self.status if isinstance(self.status, StrategyStatus) else StrategyStatus(str(self.status).upper())
        except ValueError as exc:
            raise ValueError(f"invalid strategy status: {self.status!r}") from exc

        if not strategy_id or not version or not description or not created_by:
            raise ValueError("strategy_id, version, description, and created_by are required")
        if not features:
            raise ValueError("features must not be empty")
        if not entry_rules or not exit_rules:
            raise ValueError("entry_rules and exit_rules must not be empty")
        if horizon not in {"SHORT_TERM", "SWING", "MEDIUM_TERM"}:
            raise ValueError("horizon must be SHORT_TERM, SWING, or MEDIUM_TERM")
        if not universe:
            raise ValueError("universe must not be empty")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("parameters must be a mapping")

        object.__setattr__(self, "strategy_id", strategy_id)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "features", features)
        object.__setattr__(self, "entry_rules", entry_rules)
        object.__setattr__(self, "exit_rules", exit_rules)
        object.__setattr__(self, "parameters", _freeze(self.parameters))
        object.__setattr__(self, "horizon", horizon)
        object.__setattr__(self, "universe", universe)
        object.__setattr__(self, "created_by", created_by)
        object.__setattr__(self, "created_at", _utc_iso(self.created_at, name="created_at"))
        object.__setattr__(self, "status", status)

    @property
    def key(self) -> str:
        return f"{self.strategy_id}@{self.version}"

    @property
    def selection_scope(self) -> str:
        return f"{self.horizon}|{','.join(self.universe)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "description": self.description,
            "features": list(self.features),
            "entry_rules": self.entry_rules,
            "exit_rules": self.exit_rules,
            "parameters": _thaw(self.parameters),
            "horizon": self.horizon,
            "universe": list(self.universe),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StrategyRecord":
        return cls(**dict(value))


@dataclass(frozen=True)
class PromotionEvidence:
    oos_verdict: str
    oos_sample_size: int
    oos_expectancy_r: float
    oos_profit_factor: float
    oos_max_drawdown_pct: float
    oos_sharpe: float
    parameter_stable: bool
    costs_included: bool
    regime_count: int
    recent_sample_size: int
    recent_expectancy_r: float
    shadow_sample_size: int
    shadow_expectancy_r: float
    shadow_max_drawdown_pct: float
    final_holdout_untouched: bool
    evaluation_period_locked: bool
    promotion_criteria_locked: bool
    generated_at: str
    champion_oos_expectancy_r: Optional[float] = None
    champion_shadow_expectancy_r: Optional[float] = None

    def __post_init__(self) -> None:
        verdict = str(self.oos_verdict or "").strip().upper()
        if verdict not in {"ROBUST", "MODERATE", "FRAGILE", "INSUFFICIENT_EVIDENCE"}:
            raise ValueError("oos_verdict must be a final Phase 7 verdict")
        counts = (self.oos_sample_size, self.regime_count, self.recent_sample_size, self.shadow_sample_size)
        if any(not isinstance(value, int) or value < 0 for value in counts):
            raise ValueError("evidence counts must be non-negative integers")
        numeric_names = (
            "oos_expectancy_r", "oos_profit_factor", "oos_max_drawdown_pct", "oos_sharpe",
            "recent_expectancy_r", "shadow_expectancy_r", "shadow_max_drawdown_pct",
        )
        for name in numeric_names:
            object.__setattr__(self, name, _finite(getattr(self, name), name=name))
        for name in ("champion_oos_expectancy_r", "champion_shadow_expectancy_r"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _finite(value, name=name))
        if self.oos_profit_factor < 0 or self.oos_max_drawdown_pct < 0 or self.shadow_max_drawdown_pct < 0:
            raise ValueError("profit factor and drawdown evidence must be non-negative")
        object.__setattr__(self, "oos_verdict", verdict)
        object.__setattr__(self, "generated_at", _utc_iso(self.generated_at, name="generated_at"))

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PromotionEvidence":
        return cls(**dict(value))


@dataclass(frozen=True)
class PromotionThresholds:
    min_oos_sample: int = 24
    min_oos_expectancy_r: float = 0.0
    min_oos_profit_factor: float = 1.20
    max_oos_drawdown_pct: float = 20.0
    min_oos_sharpe: float = 0.50
    min_regime_count: int = 2
    min_recent_sample: int = 12
    min_recent_expectancy_r: float = 0.0
    min_shadow_sample: int = 24
    min_shadow_expectancy_r: float = 0.0
    max_shadow_drawdown_pct: float = 20.0
    min_champion_improvement_r: float = 0.0

    def __post_init__(self) -> None:
        positive_counts = (self.min_oos_sample, self.min_regime_count, self.min_recent_sample, self.min_shadow_sample)
        if any(not isinstance(value, int) or value <= 0 for value in positive_counts):
            raise ValueError("promotion sample/regime thresholds must be positive integers")
        for name in (
            "min_oos_expectancy_r", "min_oos_profit_factor", "max_oos_drawdown_pct",
            "min_oos_sharpe", "min_recent_expectancy_r", "min_shadow_expectancy_r",
            "max_shadow_drawdown_pct", "min_champion_improvement_r",
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), name=name))
        if self.min_oos_profit_factor < 0 or self.max_oos_drawdown_pct < 0 or self.max_shadow_drawdown_pct < 0:
            raise ValueError("promotion PF/drawdown thresholds must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class PromotionDecision:
    approved: bool
    reasons: tuple[str, ...]
    checks: Mapping[str, bool]
    evidence_fingerprint: str

    def __post_init__(self) -> None:
        reasons = tuple(str(reason or "").strip() for reason in self.reasons if str(reason or "").strip())
        fingerprint = str(self.evidence_fingerprint or "").strip().lower()
        if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
            raise ValueError("evidence_fingerprint must be a SHA-256 hex digest")
        if not isinstance(self.checks, Mapping):
            raise TypeError("checks must be a mapping")
        normalized_checks = {str(name): bool(result) for name, result in self.checks.items()}
        if bool(self.approved) != all(normalized_checks.values()):
            raise ValueError("approved must equal all promotion checks")
        if self.approved and reasons:
            raise ValueError("approved decision cannot carry rejection reasons")
        if not self.approved and not reasons:
            raise ValueError("rejected decision must carry at least one reason")
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "checks", _freeze(normalized_checks))
        object.__setattr__(self, "evidence_fingerprint", fingerprint)


@dataclass(frozen=True)
class StrategyEvent:
    event_id: str
    occurred_at: str
    action: str
    strategy_id: str
    version: str
    from_status: Optional[StrategyStatus]
    to_status: StrategyStatus
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        event_id = str(self.event_id or "").strip()
        action = str(self.action or "").strip().upper()
        strategy_id = str(self.strategy_id or "").strip().upper()
        version = str(self.version or "").strip()
        if not event_id or not action or not strategy_id or not version:
            raise ValueError("event identity fields are required")
        from_status = self.from_status
        if from_status is not None and not isinstance(from_status, StrategyStatus):
            from_status = StrategyStatus(str(from_status).upper())
        to_status = self.to_status if isinstance(self.to_status, StrategyStatus) else StrategyStatus(str(self.to_status).upper())
        if not isinstance(self.details, Mapping):
            raise TypeError("event details must be a mapping")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "occurred_at", _utc_iso(self.occurred_at, name="occurred_at"))
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "strategy_id", strategy_id)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "from_status", from_status)
        object.__setattr__(self, "to_status", to_status)
        object.__setattr__(self, "details", _freeze(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "action": self.action,
            "strategy_id": self.strategy_id,
            "version": self.version,
            "from_status": self.from_status.value if self.from_status is not None else None,
            "to_status": self.to_status.value,
            "details": _thaw(self.details),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StrategyEvent":
        return cls(**dict(value))
