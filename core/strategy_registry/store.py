"""Atomic persistent store for governed strategy lifecycle records."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from core.config import DATA_DIR

from .policy import evaluate_promotion
from .types import (
    PromotionDecision,
    PromotionEvidence,
    PromotionThresholds,
    StrategyEvent,
    StrategyRecord,
    StrategyStatus,
)


SCHEMA_VERSION = 1
DEFAULT_REGISTRY_PATH = DATA_DIR / "strategy_registry.json"


class StrategyRegistryError(RuntimeError):
    """Base error for lifecycle persistence and governance failures."""


class StrategyRegistryCorruptionError(StrategyRegistryError):
    """Raised when persisted lifecycle state cannot be trusted."""


class DuplicateStrategyError(StrategyRegistryError):
    """Raised when a strategy version already exists."""


class StrategyNotFoundError(StrategyRegistryError):
    """Raised when a requested strategy version does not exist."""


class InvalidTransitionError(StrategyRegistryError):
    """Raised when lifecycle policy forbids a status transition."""


class PromotionRejectedError(StrategyRegistryError):
    """Raised when promotion evidence does not pass every locked gate."""

    def __init__(self, decision: PromotionDecision) -> None:
        self.decision = decision
        super().__init__("promotion rejected: " + ", ".join(decision.reasons))


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_id() -> str:
    return uuid.uuid4().hex


_LEGAL_TRANSITIONS: dict[StrategyStatus, frozenset[StrategyStatus]] = {
    StrategyStatus.RESEARCH: frozenset({StrategyStatus.CHALLENGER, StrategyStatus.RETIRED}),
    StrategyStatus.CHALLENGER: frozenset({StrategyStatus.SHADOW, StrategyStatus.RETIRED}),
    StrategyStatus.SHADOW: frozenset({StrategyStatus.RETIRED}),
    StrategyStatus.APPROVED: frozenset({StrategyStatus.SHADOW, StrategyStatus.RETIRED}),
    StrategyStatus.CHAMPION: frozenset(
        {StrategyStatus.DEGRADED, StrategyStatus.APPROVED, StrategyStatus.RETIRED}
    ),
    StrategyStatus.DEGRADED: frozenset(
        {StrategyStatus.SHADOW, StrategyStatus.APPROVED, StrategyStatus.RETIRED}
    ),
    StrategyStatus.RETIRED: frozenset(),
}


class StrategyRegistry:
    """Versioned lifecycle registry with fail-closed reads and atomic writes."""

    def __init__(
        self,
        path: Path | str = DEFAULT_REGISTRY_PATH,
        *,
        now_provider: Callable[[], str | datetime] = _utc_now,
        id_provider: Callable[[], str] = _event_id,
        replace_attempts: int = 3,
    ) -> None:
        self.path = Path(path)
        self._now_provider = now_provider
        self._id_provider = id_provider
        self._replace_attempts = max(1, int(replace_attempts))
        self._lock = _path_lock(self.path)

    def _now(self) -> str:
        value = self._now_provider()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat()
        return str(value)

    @staticmethod
    def _validate_invariants(records: Iterable[StrategyRecord]) -> None:
        champions: dict[str, str] = {}
        for record in records:
            if record.status is not StrategyStatus.CHAMPION:
                continue
            existing = champions.get(record.selection_scope)
            if existing is not None:
                raise ValueError(
                    f"multiple champions for scope {record.selection_scope}: "
                    f"{existing}, {record.key}"
                )
            champions[record.selection_scope] = record.key

    def _load_unlocked(self) -> tuple[dict[str, StrategyRecord], list[StrategyEvent]]:
        if not self.path.exists():
            return {}, []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("registry root must be an object")
            if raw.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unsupported registry schema version")
            raw_records = raw.get("records")
            raw_events = raw.get("events")
            if not isinstance(raw_records, dict) or not isinstance(raw_events, list):
                raise ValueError("registry records/events have invalid schema")
            records: dict[str, StrategyRecord] = {}
            for key, value in raw_records.items():
                if not isinstance(value, dict):
                    raise ValueError(f"record {key!r} is not an object")
                record = StrategyRecord.from_dict(value)
                if str(key) != record.key:
                    raise ValueError(f"record key mismatch for {key!r}")
                records[record.key] = record
            events = []
            for value in raw_events:
                if not isinstance(value, dict):
                    raise ValueError("registry event is not an object")
                events.append(StrategyEvent.from_dict(value))
            self._validate_invariants(records.values())
            return records, events
        except StrategyRegistryCorruptionError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            raise StrategyRegistryCorruptionError(
                f"strategy registry is corrupt or incompatible: {exc}"
            ) from exc

    def _write_unlocked(
        self,
        records: dict[str, StrategyRecord],
        events: list[StrategyEvent],
    ) -> None:
        self._validate_invariants(records.values())
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": self._now(),
            "records": {
                key: records[key].to_dict()
                for key in sorted(records)
            },
            "events": [event.to_dict() for event in events],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True, ensure_ascii=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            last_error: Optional[OSError] = None
            for attempt in range(self._replace_attempts):
                try:
                    os.replace(temporary, self.path)
                    last_error = None
                    break
                except PermissionError as exc:
                    last_error = exc
                    if attempt + 1 < self._replace_attempts:
                        time.sleep(0.02 * (attempt + 1))
            if last_error is not None:
                raise last_error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _event(
        self,
        *,
        action: str,
        record: StrategyRecord,
        from_status: Optional[StrategyStatus],
        to_status: StrategyStatus,
        details: dict[str, object],
    ) -> StrategyEvent:
        return StrategyEvent(
            event_id=str(self._id_provider()),
            occurred_at=self._now(),
            action=action,
            strategy_id=record.strategy_id,
            version=record.version,
            from_status=from_status,
            to_status=to_status,
            details=details,
        )

    @staticmethod
    def _normalize_status(status: StrategyStatus | str) -> StrategyStatus:
        if isinstance(status, StrategyStatus):
            return status
        return StrategyStatus(str(status).strip().upper())

    @staticmethod
    def _resolve_key(strategy_id_or_key: str, version: Optional[str]) -> str:
        identifier = str(strategy_id_or_key or "").strip()
        if version is None:
            if "@" not in identifier:
                return identifier.upper()
            strategy_id, stored_version = identifier.split("@", 1)
            return f"{strategy_id.strip().upper()}@{stored_version.strip()}"
        return f"{identifier.upper()}@{str(version).strip()}"

    def register(self, record: StrategyRecord, *, actor: Optional[str] = None) -> StrategyRecord:
        if not isinstance(record, StrategyRecord):
            raise TypeError("record must be a StrategyRecord")
        if record.status is not StrategyStatus.RESEARCH:
            raise InvalidTransitionError("new strategy versions must start in RESEARCH")
        with self._lock:
            records, events = self._load_unlocked()
            if record.key in records:
                raise DuplicateStrategyError(f"strategy version already registered: {record.key}")
            records[record.key] = record
            events.append(
                self._event(
                    action="REGISTER",
                    record=record,
                    from_status=None,
                    to_status=StrategyStatus.RESEARCH,
                    details={"actor": str(actor or record.created_by)},
                )
            )
            self._write_unlocked(records, events)
        return record

    def get(self, strategy_id_or_key: str, version: Optional[str] = None) -> StrategyRecord:
        key = self._resolve_key(strategy_id_or_key, version)
        with self._lock:
            records, _ = self._load_unlocked()
            try:
                return records[key]
            except KeyError as exc:
                raise StrategyNotFoundError(f"strategy version not found: {key}") from exc

    def list_records(
        self,
        *,
        status: StrategyStatus | str | None = None,
    ) -> list[StrategyRecord]:
        normalized = self._normalize_status(status) if status is not None else None
        with self._lock:
            records, _ = self._load_unlocked()
        selected = (
            record for record in records.values()
            if normalized is None or record.status is normalized
        )
        return sorted(selected, key=lambda item: (item.strategy_id, item.version))

    def history(self) -> list[StrategyEvent]:
        with self._lock:
            _, events = self._load_unlocked()
            return list(events)

    def champion_for_scope(self, selection_scope: str) -> Optional[StrategyRecord]:
        scope = str(selection_scope or "").strip().upper()
        with self._lock:
            records, _ = self._load_unlocked()
        matches = [
            record for record in records.values()
            if record.status is StrategyStatus.CHAMPION and record.selection_scope == scope
        ]
        if len(matches) > 1:
            raise StrategyRegistryCorruptionError(f"multiple champions for scope {scope}")
        return matches[0] if matches else None

    def active_record(self, strategy_id: str) -> Optional[StrategyRecord]:
        """Return an executable CHAMPION/APPROVED version, preferring CHAMPION."""

        identifier = str(strategy_id or "").strip().upper()
        with self._lock:
            records, _ = self._load_unlocked()
        candidates = [
            record for record in records.values()
            if record.strategy_id == identifier
            and record.status in {StrategyStatus.CHAMPION, StrategyStatus.APPROVED}
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                item.status is StrategyStatus.CHAMPION,
                item.created_at,
                item.version,
            ),
        )

    def transition(
        self,
        strategy_key: str,
        to_status: StrategyStatus | str,
        *,
        actor: str,
        reason: str,
    ) -> StrategyRecord:
        target = self._normalize_status(to_status)
        if target is StrategyStatus.CHAMPION:
            raise InvalidTransitionError("CHAMPION can only be assigned by evidence-backed promote()")
        with self._lock:
            records, events = self._load_unlocked()
            key = self._resolve_key(strategy_key, None)
            try:
                current = records[key]
            except KeyError as exc:
                raise StrategyNotFoundError(f"strategy version not found: {key}") from exc
            if target not in _LEGAL_TRANSITIONS[current.status]:
                raise InvalidTransitionError(
                    f"transition {current.status.value} -> {target.value} is not allowed"
                )
            updated = replace(current, status=target)
            records[key] = updated
            events.append(
                self._event(
                    action="TRANSITION",
                    record=updated,
                    from_status=current.status,
                    to_status=target,
                    details={"actor": str(actor), "reason": str(reason)},
                )
            )
            self._write_unlocked(records, events)
            return updated

    def governance_repair_to_research(
        self,
        strategy_key: str,
        *,
        actor: str,
        reason: str,
    ) -> StrategyRecord:
        """Repair an invalid automatic CHALLENGER state without opening a general downgrade path."""
        if str(actor or "").strip() != "research_lifecycle_guard":
            raise InvalidTransitionError("governance repair requires research_lifecycle_guard")
        if not str(reason or "").strip():
            raise InvalidTransitionError("governance repair requires an audited reason")
        with self._lock:
            records, events = self._load_unlocked()
            key = self._resolve_key(strategy_key, None)
            try:
                current = records[key]
            except KeyError as exc:
                raise StrategyNotFoundError(f"strategy version not found: {key}") from exc
            if current.status is not StrategyStatus.CHALLENGER:
                raise InvalidTransitionError(
                    "governance repair is limited to CHALLENGER -> RESEARCH"
                )
            updated = replace(current, status=StrategyStatus.RESEARCH)
            records[key] = updated
            events.append(
                self._event(
                    action="GOVERNANCE_REPAIR",
                    record=updated,
                    from_status=current.status,
                    to_status=StrategyStatus.RESEARCH,
                    details={"actor": str(actor), "reason": str(reason)},
                )
            )
            self._write_unlocked(records, events)
            return updated

    def promote(
        self,
        strategy_key: str,
        evidence: PromotionEvidence,
        *,
        thresholds: PromotionThresholds = PromotionThresholds(),
        actor: str,
    ) -> tuple[StrategyRecord, PromotionDecision]:
        if not isinstance(evidence, PromotionEvidence):
            raise TypeError("evidence must be PromotionEvidence")
        if not isinstance(thresholds, PromotionThresholds):
            raise TypeError("thresholds must be PromotionThresholds")
        with self._lock:
            records, events = self._load_unlocked()
            key = self._resolve_key(strategy_key, None)
            try:
                candidate = records[key]
            except KeyError as exc:
                raise StrategyNotFoundError(f"strategy version not found: {key}") from exc
            if candidate.status is not StrategyStatus.SHADOW:
                raise InvalidTransitionError("only a SHADOW strategy can be promoted")

            previous = next(
                (
                    record for record in records.values()
                    if record.status is StrategyStatus.CHAMPION
                    and record.selection_scope == candidate.selection_scope
                    and record.key != candidate.key
                ),
                None,
            )
            decision = evaluate_promotion(
                evidence,
                thresholds=thresholds,
                champion_exists=previous is not None,
            )
            if not decision.approved:
                raise PromotionRejectedError(decision)

            if previous is not None:
                demoted = replace(previous, status=StrategyStatus.APPROVED)
                records[previous.key] = demoted
                events.append(
                    self._event(
                        action="DEMOTE",
                        record=demoted,
                        from_status=StrategyStatus.CHAMPION,
                        to_status=StrategyStatus.APPROVED,
                        details={"actor": str(actor), "replacement": candidate.key},
                    )
                )

            champion = replace(candidate, status=StrategyStatus.CHAMPION)
            records[candidate.key] = champion
            events.append(
                self._event(
                    action="PROMOTE",
                    record=champion,
                    from_status=StrategyStatus.SHADOW,
                    to_status=StrategyStatus.CHAMPION,
                    details={
                        "actor": str(actor),
                        "previous_champion": previous.key if previous is not None else None,
                        "evidence_fingerprint": decision.evidence_fingerprint,
                        "checks": dict(decision.checks),
                        "evidence": evidence.to_dict(),
                        "thresholds": thresholds.to_dict(),
                    },
                )
            )
            self._write_unlocked(records, events)
            return champion, decision


__all__ = [
    "DEFAULT_REGISTRY_PATH",
    "DuplicateStrategyError",
    "InvalidTransitionError",
    "PromotionRejectedError",
    "SCHEMA_VERSION",
    "StrategyNotFoundError",
    "StrategyRegistry",
    "StrategyRegistryCorruptionError",
    "StrategyRegistryError",
]
