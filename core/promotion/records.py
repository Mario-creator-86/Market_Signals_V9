"""Phase 16 §87 — promotion records with rollback conditions.

Every promotion stores previous/new version, evidence, rollback conditions
and status (ACTIVE / ROLLED_BACK). The rollback monitor restores the last
known valid champion when a configured regression threshold is breached —
deterministically, and never by weakening risk limits (§48/GOLD-016 remain
authoritative).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import DATA_DIR

DEFAULT_PROMOTION_RECORDS_PATH = DATA_DIR / "promotion_records.json"
SCHEMA_VERSION = 1

# §87 rollback condition kinds the monitor understands (deterministic only).
ROLLBACK_CONDITION_KINDS = frozenset({
    "expectancy_r_below", "profit_factor_below", "drawdown_above", "sample_minimum",
})


class PromotionRecordError(ValueError):
    """Invalid promotion record or transition."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_promotion_records_path() -> Path:
    override = os.environ.get("MS_PROMOTION_RECORDS_PATH", "").strip()
    return Path(override) if override else DEFAULT_PROMOTION_RECORDS_PATH


def validate_rollback_condition(condition: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(condition.get("kind") or "").strip()
    if kind not in ROLLBACK_CONDITION_KINDS:
        raise PromotionRecordError(f"unknown rollback condition kind: {kind}")
    try:
        threshold = float(condition.get("threshold"))
    except (TypeError, ValueError) as exc:
        raise PromotionRecordError("rollback condition requires a numeric threshold") from exc
    window = int(condition.get("window", 0) or 0)
    if window < 0:
        raise PromotionRecordError("rollback window must be >= 0")
    return {"kind": kind, "threshold": threshold, "window": window}


@dataclass(frozen=True)
class PromotionRecord:
    promotion_id: str
    experiment_id: str
    strategy_id: str
    previous_version: str
    new_version: str
    promotion_time: str
    promotion_evidence: Dict[str, Any]
    rollback_conditions: List[Dict[str, Any]]
    status: str = "ACTIVE"           # ACTIVE | ROLLED_BACK
    rolled_back_at: Optional[str] = None
    rollback_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "promotion_id": self.promotion_id, "experiment_id": self.experiment_id,
            "strategy_id": self.strategy_id, "previous_version": self.previous_version,
            "new_version": self.new_version, "promotion_time": self.promotion_time,
            "promotion_evidence": dict(self.promotion_evidence),
            "rollback_conditions": [dict(c) for c in self.rollback_conditions],
            "status": self.status, "rolled_back_at": self.rolled_back_at,
            "rollback_reason": self.rollback_reason,
        }

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "PromotionRecord":
        known = set(PromotionRecord.__dataclass_fields__)
        return PromotionRecord(**{k: v for k, v in (raw or {}).items() if k in known})


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


class PromotionRecordStore:
    """Atomic JSON store; append promotions, roll back at most once each."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else default_promotion_records_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict) and isinstance(payload.get("promotions"), list):
                return payload
        except (OSError, ValueError):
            pass
        return {"schema_version": SCHEMA_VERSION, "promotions": []}

    def _save(self, payload: Dict[str, Any]) -> None:
        with _path_lock(self.path):
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    def append(
        self, *, experiment_id: str, strategy_id: str, previous_version: str,
        new_version: str, promotion_evidence: Dict[str, Any],
        rollback_conditions: List[Dict[str, Any]],
    ) -> PromotionRecord:
        conditions = [validate_rollback_condition(dict(c)) for c in (rollback_conditions or [])]
        if not conditions:
            raise PromotionRecordError("§87: every promotion must record rollback conditions")
        for required in ("experiment_id", "strategy_id", "new_version"):
            if not str(locals().get(required, "") or "").strip():
                raise PromotionRecordError(f"promotion requires {required}")
        record = PromotionRecord(
            promotion_id=f"promo_{uuid.uuid4().hex[:12]}",
            experiment_id=str(experiment_id), strategy_id=str(strategy_id),
            previous_version=str(previous_version or ""), new_version=str(new_version),
            promotion_time=_utc_now(), promotion_evidence=dict(promotion_evidence or {}),
            rollback_conditions=conditions,
        )
        with _path_lock(self.path):
            payload = self._load()
            payload["promotions"].append(record.to_dict())
            self._save(payload)
        return record

    def get(self, promotion_id: str) -> Optional[PromotionRecord]:
        for raw in self._load()["promotions"]:
            if raw.get("promotion_id") == promotion_id:
                return PromotionRecord.from_dict(raw)
        return None

    def list_records(self, *, limit: int = 100, strategy_id: Optional[str] = None) -> List[PromotionRecord]:
        records = [PromotionRecord.from_dict(raw) for raw in self._load()["promotions"]]
        if strategy_id:
            records = [r for r in records if r.strategy_id == str(strategy_id).upper()]
        return list(reversed(records[-max(1, min(500, int(limit or 100))):]))

    def last_active_for_strategy(self, strategy_id: str) -> Optional[PromotionRecord]:
        for record in reversed(self.list_records(limit=500, strategy_id=strategy_id)):
            if record.status == "ACTIVE":
                return record
        return None

    def mark_rolled_back(self, promotion_id: str, *, reason: str) -> PromotionRecord:
        with _path_lock(self.path):
            payload = self._load()
            for index, raw in enumerate(payload["promotions"]):
                if raw.get("promotion_id") != promotion_id:
                    continue
                record = PromotionRecord.from_dict(raw)
                if record.status == "ROLLED_BACK":
                    return record  # idempotent — never roll back twice
                updated = PromotionRecord(**{
                    **record.to_dict(), "status": "ROLLED_BACK",
                    "rolled_back_at": _utc_now(), "rollback_reason": str(reason or ""),
                })
                payload["promotions"][index] = updated.to_dict()
                self._save(payload)
                return updated
        raise PromotionRecordError(f"unknown promotion_id: {promotion_id}")


__all__ = [
    "PromotionRecord", "PromotionRecordStore", "PromotionRecordError",
    "validate_rollback_condition", "ROLLBACK_CONDITION_KINDS",
    "default_promotion_records_path",
]
