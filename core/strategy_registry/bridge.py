"""Bridge the executable V2 catalog into governed V9 lifecycle records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from core.strategy_v2.registry import (
    StrategyRegistry as ExecutableStrategyRegistry,
    StrategySpec,
    build_default_registry,
)

from .store import DuplicateStrategyError, StrategyRegistry
from .types import StrategyRecord, StrategyStatus


V2_LIFECYCLE_VERSION = "2.0.0"
V2_HORIZONS = {
    "S1_TREND_PULLBACK": "SWING",
    "S2_BREAKOUT_RETEST": "SHORT_TERM",
    "S3_VOL_SQUEEZE": "SWING",
    "S4_MEAN_REVERSION": "SWING",
    "S5_MOMENTUM_CONTINUATION": "SHORT_TERM",
    "S6_FX_SESSION_BREAKOUT": "SHORT_TERM",
    "S7_LIQUIDITY_SWEEP_REVERSAL": "SHORT_TERM",
    "S8_SUPERTREND_BOS": "SWING",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_from_v2_spec(
    spec: StrategySpec,
    *,
    version: str = V2_LIFECYCLE_VERSION,
    created_by: str = "v2_catalog_migration",
    created_at: Optional[str] = None,
) -> StrategyRecord:
    """Map one executable definition without claiming validation or approval."""

    if not isinstance(spec, StrategySpec):
        raise TypeError("spec must be a StrategySpec")
    strategy_id = str(spec.id or "").strip().upper()
    try:
        horizon = V2_HORIZONS[strategy_id]
    except KeyError as exc:
        raise ValueError(f"no locked lifecycle horizon for V2 strategy {strategy_id!r}") from exc
    description = str(spec.name or "").strip()
    notes = str(spec.notes or "").strip()
    if notes:
        description = f"{description}. {notes}"
    return StrategyRecord(
        strategy_id=strategy_id,
        version=version,
        description=description,
        features=tuple(spec.features_required),
        entry_rules=spec.entry_rules,
        exit_rules=spec.exit_rules,
        parameters=spec.params_default_by_asset,
        horizon=horizon,
        universe=tuple(spec.asset_classes),
        created_by=created_by,
        created_at=created_at or _utc_now(),
        status=StrategyStatus.RESEARCH,
    )


def seed_v2_registry(
    lifecycle_registry: StrategyRegistry,
    *,
    executable_registry: Optional[ExecutableStrategyRegistry] = None,
    version: str = V2_LIFECYCLE_VERSION,
    created_by: str = "v2_catalog_migration",
    created_at: Optional[str] = None,
) -> list[StrategyRecord]:
    """Idempotently seed every current V2 definition as RESEARCH."""

    if not isinstance(lifecycle_registry, StrategyRegistry):
        raise TypeError("lifecycle_registry must be a lifecycle StrategyRegistry")
    executable = executable_registry or build_default_registry()
    existing = {record.key for record in lifecycle_registry.list_records()}
    created: list[StrategyRecord] = []
    seed_time = created_at or _utc_now()
    for spec in executable.specs:
        record = record_from_v2_spec(
            spec,
            version=version,
            created_by=created_by,
            created_at=seed_time,
        )
        if record.key in existing:
            continue
        try:
            created.append(lifecycle_registry.register(record, actor=created_by))
        except DuplicateStrategyError:
            # Another seeder may have registered the same immutable version.
            continue
        existing.add(record.key)
    return created


__all__ = [
    "V2_HORIZONS",
    "V2_LIFECYCLE_VERSION",
    "record_from_v2_spec",
    "seed_v2_registry",
]
