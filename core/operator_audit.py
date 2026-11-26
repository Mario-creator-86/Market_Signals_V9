"""Tamper-evident, append-only audit ledger for operator API mutations."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence


AUDIT_SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64


class OperatorAuditError(RuntimeError):
    """Raised when operator evidence cannot be written or verified safely."""


class OperatorAuditVerificationError(OperatorAuditError):
    """Raised when an existing ledger has been modified or corrupted."""


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _clean_text(value: object, *, maximum: int) -> str:
    return (
        str(value or "")
        .replace("\x00", "")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()[:maximum]
    )


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class OperatorAuditLedger:
    """Durably records a cryptographic chain without request or response bodies."""

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        key: Optional[str] = None,
        previous_keys: Optional[Sequence[str]] = None,
        required: Optional[bool] = None,
    ):
        deploy_env = str(os.getenv("MS_ENV", "development") or "development").lower()
        self.required = (
            deploy_env in {"production", "prod"}
            if required is None
            else bool(required)
        )
        self._key_text = str(
            key if key is not None else os.getenv("MS_AUDIT_HMAC_KEY", "")
        ).strip()
        self.enabled = bool(self._key_text)
        self._key = self._key_text.encode("utf-8")
        self.key_id = (
            hashlib.sha256(self._key).hexdigest()[:16]
            if self.enabled
            else None
        )
        raw_previous = (
            list(previous_keys)
            if previous_keys is not None
            else str(os.getenv("MS_AUDIT_HMAC_PREVIOUS_KEYS", "") or "").split(",")
        )
        self._verification_keys: Dict[str, bytes] = {}
        for candidate in [self._key_text, *raw_previous]:
            candidate_text = str(candidate or "").strip()
            if not candidate_text:
                continue
            candidate_bytes = candidate_text.encode("utf-8")
            candidate_id = hashlib.sha256(candidate_bytes).hexdigest()[:16]
            existing = self._verification_keys.get(candidate_id)
            if existing is not None and not hmac.compare_digest(
                existing,
                candidate_bytes,
            ):
                raise OperatorAuditError("Operator audit key identifier collision.")
            self._verification_keys[candidate_id] = candidate_bytes
        data_dir = Path(
            os.getenv("MS_DATA_DIR")
            or (Path(__file__).resolve().parent.parent / "data")
        )
        self.path = Path(
            path
            or os.getenv("MS_OPERATOR_AUDIT_PATH")
            or (data_dir / "operator_audit.jsonl")
        ).resolve()
        self.lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        self._thread_lock = threading.RLock()
        self._record_count = 0
        self._last_hash = GENESIS_HASH
        self._last_verified_at: Optional[str] = None
        self._last_write_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._marker: Optional[tuple[int, int]] = None

    def _digest(
        self,
        payload: Mapping[str, Any],
        *,
        signing_key: Optional[bytes] = None,
    ) -> str:
        key = signing_key
        if key is None:
            record_key_id = str(payload.get("key_id") or "")
            key = self._verification_keys.get(record_key_id)
            if key is None:
                raise OperatorAuditVerificationError(
                    f"No verification key is configured for key_id={record_key_id!r}."
                )
        return hmac.new(key, _canonical_bytes(payload), hashlib.sha256).hexdigest()

    def _path_marker(self) -> Optional[tuple[int, int]]:
        try:
            stat = self.path.stat()
            return int(stat.st_size), int(stat.st_mtime_ns)
        except FileNotFoundError:
            return None

    def _verify_locked(self) -> Dict[str, Any]:
        if not self.enabled:
            if self.required:
                raise OperatorAuditError(
                    "MS_AUDIT_HMAC_KEY is required for the operator audit ledger."
                )
            return {
                "verified": False,
                "enabled": False,
                "record_count": 0,
                "last_hash": None,
                "reason": "audit_key_not_configured",
            }

        previous_hash = GENESIS_HASH
        expected_sequence = 1
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    for line_number, raw_line in enumerate(handle, start=1):
                        line = raw_line.rstrip("\r\n")
                        if not line:
                            raise OperatorAuditVerificationError(
                                f"Blank audit record at line {line_number}."
                            )
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise OperatorAuditVerificationError(
                                f"Invalid JSON at audit line {line_number}: {exc}"
                            ) from exc
                        if not isinstance(record, dict):
                            raise OperatorAuditVerificationError(
                                f"Audit line {line_number} is not an object."
                            )
                        if int(record.get("schema_version", 0) or 0) != AUDIT_SCHEMA_VERSION:
                            raise OperatorAuditVerificationError(
                                f"Unsupported audit schema at line {line_number}."
                            )
                        if int(record.get("sequence", 0) or 0) != expected_sequence:
                            raise OperatorAuditVerificationError(
                                f"Audit sequence mismatch at line {line_number}."
                            )
                        if not hmac.compare_digest(
                            str(record.get("previous_hash") or ""),
                            previous_hash,
                        ):
                            raise OperatorAuditVerificationError(
                                f"Audit chain mismatch at line {line_number}."
                            )
                        actual_hash = str(record.get("entry_hash") or "")
                        unsigned = dict(record)
                        unsigned.pop("entry_hash", None)
                        expected_hash = self._digest(unsigned)
                        if not hmac.compare_digest(actual_hash, expected_hash):
                            raise OperatorAuditVerificationError(
                                f"Audit HMAC mismatch at line {line_number}."
                            )
                        previous_hash = actual_hash
                        expected_sequence += 1
            except OSError as exc:
                raise OperatorAuditError(f"Cannot read operator audit ledger: {exc}") from exc

        verified_at = datetime.now(timezone.utc).isoformat()
        self._record_count = expected_sequence - 1
        self._last_hash = previous_hash
        self._last_verified_at = verified_at
        self._last_error = None
        self._marker = self._path_marker()
        return {
            "verified": True,
            "enabled": True,
            "path": str(self.path),
            "key_id": self.key_id,
            "verification_key_count": len(self._verification_keys),
            "record_count": self._record_count,
            "last_hash": self._last_hash,
            "verified_at": verified_at,
        }

    def _sync_locked(self, *, force: bool = False) -> Dict[str, Any]:
        marker = self._path_marker()
        if (
            not force
            and self._last_verified_at is not None
            and marker == self._marker
        ):
            return {
                "verified": True,
                "enabled": True,
                "path": str(self.path),
                "key_id": self.key_id,
                "verification_key_count": len(self._verification_keys),
                "record_count": self._record_count,
                "last_hash": self._last_hash,
                "verified_at": self._last_verified_at,
            }
        return self._verify_locked()

    def ensure_ready(self) -> bool:
        if not self.enabled:
            return not self.required
        try:
            with self._thread_lock, _exclusive_file_lock(self.lock_path):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a+b") as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
                self._sync_locked()
            return True
        except OperatorAuditError as exc:
            self._last_error = str(exc)
            return False
        except OSError as exc:
            self._last_error = f"Operator audit storage unavailable: {exc}"
            return False

    def append_http_event(
        self,
        *,
        request_id: str,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
        actor: str,
        client_address: str,
    ) -> Dict[str, Any]:
        if not self.enabled:
            if self.required:
                raise OperatorAuditError("Operator audit is required but disabled.")
            return {"recorded": False, "reason": "audit_key_not_configured"}

        with self._thread_lock, _exclusive_file_lock(self.lock_path):
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._sync_locked()
                sequence = self._record_count + 1
                status = max(100, min(599, int(status_code)))
                method_value = _clean_text(method, maximum=12).upper()
                path_value = _clean_text(path, maximum=512)
                actor_value = _clean_text(actor, maximum=64) or "unknown"
                client_value = _clean_text(client_address, maximum=256)
                record: Dict[str, Any] = {
                    "schema_version": AUDIT_SCHEMA_VERSION,
                    "event_id": uuid.uuid4().hex,
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "sequence": sequence,
                    "previous_hash": self._last_hash,
                    "key_id": self.key_id,
                    "request_id": _clean_text(request_id, maximum=128),
                    "method": method_value,
                    "path": path_value,
                    "status_code": status,
                    "outcome": (
                        "success"
                        if status < 400
                        else "denied"
                        if status in {401, 403, 429}
                        else "failed"
                    ),
                    "duration_ms": round(max(0.0, float(duration_ms)), 3),
                    "actor": actor_value,
                    "client_fingerprint": hmac.new(
                        self._key,
                        f"client:{client_value}".encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()[:24],
                }
                record["entry_hash"] = self._digest(record, signing_key=self._key)
                serialized = _canonical_bytes(record) + b"\n"
                with self.path.open("ab") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._record_count = sequence
                self._last_hash = str(record["entry_hash"])
                self._last_write_at = str(record["occurred_at"])
                self._last_verified_at = self._last_write_at
                self._last_error = None
                self._marker = self._path_marker()
                return {
                    "recorded": True,
                    "sequence": sequence,
                    "entry_hash": self._last_hash,
                    "event_id": record["event_id"],
                }
            except (OSError, ValueError, TypeError) as exc:
                self._last_error = f"Operator audit append failed: {exc}"
                raise OperatorAuditError(self._last_error) from exc
            except OperatorAuditError as exc:
                self._last_error = str(exc)
                raise

    def copy_verified_snapshot(self, destination: Path) -> Dict[str, Any]:
        """Copy a stable ledger while holding the same cross-process write lock."""
        if not self.enabled:
            raise OperatorAuditError(
                "MS_AUDIT_HMAC_KEY is required to snapshot operator audit evidence."
            )
        target = Path(destination).resolve()
        with self._thread_lock, _exclusive_file_lock(self.lock_path):
            verification = self._sync_locked(force=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.path.open("rb") as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
            except OSError as exc:
                raise OperatorAuditError(
                    f"Cannot snapshot operator audit ledger: {exc}"
                ) from exc
            source_digest = _sha256_path(self.path)
            target_digest = _sha256_path(target)
            if not hmac.compare_digest(source_digest, target_digest):
                target.unlink(missing_ok=True)
                raise OperatorAuditError("Operator audit snapshot digest mismatch.")
            return {
                **verification,
                "snapshot_sha256": target_digest,
            }

    def verify(self) -> Dict[str, Any]:
        try:
            with self._thread_lock, _exclusive_file_lock(self.lock_path):
                return self._sync_locked(force=True)
        except OperatorAuditError as exc:
            self._last_error = str(exc)
            return {
                "verified": False,
                "enabled": self.enabled,
                "path": str(self.path),
                "key_id": self.key_id,
                "record_count": self._record_count,
                "last_hash": self._last_hash if self.enabled else None,
                "error": str(exc),
            }
        except OSError as exc:
            self._last_error = f"Operator audit verification failed: {exc}"
            return {
                "verified": False,
                "enabled": self.enabled,
                "path": str(self.path),
                "key_id": self.key_id,
                "record_count": self._record_count,
                "last_hash": self._last_hash if self.enabled else None,
                "error": self._last_error,
            }

    def status(self) -> Dict[str, Any]:
        ready = self.ensure_ready()
        return {
            "enabled": self.enabled,
            "required": self.required,
            "ready": ready,
            "path": str(self.path),
            "key_id": self.key_id,
            "verification_key_count": len(self._verification_keys),
            "record_count": self._record_count,
            "last_hash": self._last_hash if self.enabled else None,
            "last_verified_at": self._last_verified_at,
            "last_write_at": self._last_write_at,
            "last_error": self._last_error,
        }


_LEDGER: Optional[OperatorAuditLedger] = None
_LEDGER_LOCK = threading.Lock()


def get_operator_audit_ledger() -> OperatorAuditLedger:
    global _LEDGER
    with _LEDGER_LOCK:
        if _LEDGER is None:
            _LEDGER = OperatorAuditLedger()
        return _LEDGER


def reset_operator_audit_ledger() -> None:
    global _LEDGER
    with _LEDGER_LOCK:
        _LEDGER = None
