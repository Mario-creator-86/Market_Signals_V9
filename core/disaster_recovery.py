"""Verified backup, retention, and restore primitives for runtime state."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence


logger = logging.getLogger("DisasterRecovery")

BASE_DIR = Path(__file__).resolve().parent.parent
BACKUP_SCHEMA_VERSION = 1
BACKUP_PREFIX = "market-signals-backup-"


class BackupError(RuntimeError):
    """Base error for backup and restore operations."""


class BackupVerificationError(BackupError):
    """Raised when an archive cannot prove its own integrity."""


class RestoreSafetyError(BackupError):
    """Raised when a destructive restore lacks an explicit safety boundary."""


@dataclass(frozen=True)
class FileSpec:
    restore_path: str
    source_path: Path
    required: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(dt: Optional[datetime] = None) -> str:
    return (dt or _utc_now()).isoformat()


def _truthy(value: object, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return bool(default)
    return text in {"1", "true", "yes", "on"}


def _bounded_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


def _sha256_path(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _safe_relative_path(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    reserved_windows_names = {
        "con", "prn", "aux", "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
    if (
        not normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any("\x00" in part or ":" in part for part in path.parts)
        or any(
            part.rstrip(" .").split(".", 1)[0].lower() in reserved_windows_names
            for part in path.parts
        )
    ):
        raise BackupVerificationError(f"Unsafe archive path: {value!r}")
    return path.as_posix()


def _safe_label(value: Optional[str]) -> str:
    text = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip()).strip("-")
    return text[:40]


def _sqlite_integrity(path: Path) -> Dict[str, Any]:
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=15.0)
        try:
            rows = [
                str(row[0])
                for row in connection.execute("PRAGMA integrity_check").fetchall()
            ]
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise BackupVerificationError(f"SQLite integrity check failed: {exc}") from exc
    ok = rows == ["ok"]
    if not ok:
        raise BackupVerificationError(
            f"SQLite integrity check returned: {rows[:10]}"
        )
    return {"ok": True, "result": rows, "tables": tables}


class BackupManager:
    """Create and verify portable, point-in-time runtime snapshots."""

    def __init__(
        self,
        *,
        project_root: Optional[Path] = None,
        database_path: Optional[Path] = None,
        backup_dir: Optional[Path] = None,
        file_specs: Optional[Sequence[FileSpec]] = None,
        retention_count: Optional[int] = None,
        retention_days: Optional[int] = None,
        allow_restore: bool = False,
    ):
        self.project_root = Path(project_root or BASE_DIR).resolve()
        # The instance's own data folder (MS_DATA_DIR), so an instance started
        # with its own folders never backs up, or prunes the backups of, another.
        default_data_dir = Path(os.getenv("MS_DATA_DIR") or (self.project_root / "data"))
        self.database_path = Path(
            database_path
            or os.getenv("MS_SQLITE_DB_PATH")
            or (default_data_dir / "market_signals.db")
        ).resolve()
        self.backup_dir = Path(
            backup_dir
            or os.getenv("MS_BACKUP_DIR")
            or (default_data_dir / "backups")
        ).resolve()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.file_specs = list(file_specs or self._default_file_specs())
        self.retention_count = _bounded_int(
            retention_count
            if retention_count is not None
            else os.getenv("MS_BACKUP_RETENTION_COUNT", "7"),
            default=7,
            minimum=1,
            maximum=365,
        )
        self.retention_days = _bounded_int(
            retention_days
            if retention_days is not None
            else os.getenv("MS_BACKUP_RETENTION_DAYS", "30"),
            default=30,
            minimum=1,
            maximum=3650,
        )
        self.allow_restore = bool(allow_restore)
        self._lock = threading.RLock()

    def _default_file_specs(self) -> List[FileSpec]:
        root = self.project_root
        runtime_dir = Path(os.getenv("MS_RUNTIME_DIR") or root)

        def runtime_source(env_name: str, filename: str) -> Path:
            return Path(os.getenv(env_name) or (runtime_dir / filename)).resolve()

        def portable_restore_path(source: Path, filename: str) -> str:
            try:
                return _safe_relative_path(source.relative_to(root).as_posix())
            except ValueError:
                return f"runtime/{filename}"

        config_source = runtime_source("MS_CONFIG_PATH", "config.json")
        watchlist_source = runtime_source("MS_WATCHLIST_PATH", "watchlist.json")
        state_source = runtime_source("MS_STATE_PATH", "state.json")
        signals_source = runtime_source("MS_SIGNALS_PATH", "signals_state.json")
        capital_source = runtime_source("MS_CAPITAL_STATE_PATH", "capital.json")
        data_dir = Path(os.getenv("MS_DATA_DIR") or (root / "data")).resolve()
        operator_audit_source = Path(
            os.getenv("MS_OPERATOR_AUDIT_PATH")
            or (data_dir / "operator_audit.jsonl")
        ).resolve()
        specs = [
            FileSpec(
                portable_restore_path(config_source, "config.json"),
                config_source,
                required=True,
            ),
            FileSpec(
                portable_restore_path(watchlist_source, "watchlist.json"),
                watchlist_source,
                required=True,
            ),
            FileSpec(
                portable_restore_path(state_source, "state.json"),
                state_source,
                required=True,
            ),
            FileSpec(
                portable_restore_path(signals_source, "signals_state.json"),
                signals_source,
                required=True,
            ),
            FileSpec(
                portable_restore_path(capital_source, "capital.json"),
                capital_source,
                required=True,
            ),
            FileSpec(
                "data/trade_decisions.json",
                data_dir / "trade_decisions.json",
            ),
            FileSpec(
                "data/trade_journal.json",
                data_dir / "trade_journal.json",
            ),
            FileSpec(
                "data/research_cohort_journal.json",
                data_dir / "research_cohort_journal.json",
            ),
            FileSpec(
                "data/executions_crypto.json.append.jsonl",
                data_dir / "executions_crypto.json.append.jsonl",
            ),
            FileSpec(
                "data/executions_stocks.json.append.jsonl",
                data_dir / "executions_stocks.json.append.jsonl",
            ),
            FileSpec(
                "data/news_evidence_ledger.jsonl",
                data_dir / "news_evidence_ledger.jsonl",
            ),
            FileSpec(
                portable_restore_path(
                    operator_audit_source,
                    "operator_audit.jsonl",
                ),
                operator_audit_source,
            ),
        ]
        if _truthy(os.getenv("MS_BACKUP_INCLUDE_NEWS_ARCHIVE"), False):
            news_source = Path(
                os.getenv("MS_NEWS_BACKUP_PATH")
                or (data_dir / "news_backup.json")
            ).resolve()
            specs.append(
                FileSpec(
                    portable_restore_path(news_source, "news_backup.json"),
                    news_source,
                )
            )
        return specs

    def _database_restore_path(self) -> str:
        try:
            relative = self.database_path.relative_to(self.project_root)
            return _safe_relative_path(relative.as_posix())
        except ValueError:
            return f"data/{self.database_path.name}"

    def _backup_sqlite(self, destination: Path) -> Dict[str, Any]:
        if not self.database_path.exists():
            raise BackupError(f"SQLite database does not exist: {self.database_path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(str(self.database_path), timeout=30.0)
        target = sqlite3.connect(str(destination), timeout=30.0)
        try:
            source.execute("PRAGMA busy_timeout=5000")
            source.backup(target, pages=1024, sleep=0.01)
            target.commit()
        except sqlite3.Error as exc:
            raise BackupError(f"SQLite online backup failed: {exc}") from exc
        finally:
            target.close()
            source.close()
        return _sqlite_integrity(destination)

    def _entry_for_path(
        self,
        *,
        archive_path: str,
        restore_path: str,
        source: Path,
        kind: str,
        required: bool,
    ) -> Dict[str, Any]:
        digest, size = _sha256_path(source)
        return {
            "archive_path": _safe_relative_path(archive_path),
            "restore_path": _safe_relative_path(restore_path),
            "kind": kind,
            "required": bool(required),
            "size": size,
            "sha256": digest,
        }

    def create_snapshot(
        self,
        *,
        label: Optional[str] = None,
        retention_keep: Optional[Sequence[Path | str]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            created_at = _utc_now()
            snapshot_id = uuid.uuid4().hex
            timestamp = created_at.strftime("%Y%m%dT%H%M%S%fZ")
            suffix = _safe_label(label)
            filename = f"{BACKUP_PREFIX}{timestamp}-{snapshot_id[:8]}"
            if suffix:
                filename += f"-{suffix}"
            filename += ".zip"
            final_path = self.backup_dir / filename
            partial_path = self.backup_dir / f".{filename}.partial"
            staging_path = Path(
                tempfile.mkdtemp(prefix=".snapshot-", dir=str(self.backup_dir))
            ).resolve()
            try:
                staging_path.relative_to(self.backup_dir)
                database_copy = staging_path / "database.sqlite"
                sqlite_check = self._backup_sqlite(database_copy)
                entries: List[Dict[str, Any]] = [
                    self._entry_for_path(
                        archive_path="payload/database.sqlite",
                        restore_path=self._database_restore_path(),
                        source=database_copy,
                        kind="sqlite",
                        required=True,
                    )
                ]
                staged_files: List[tuple[Path, str]] = [
                    (database_copy, "payload/database.sqlite")
                ]
                operator_audit_source = Path(
                    os.getenv("MS_OPERATOR_AUDIT_PATH")
                    or (
                        Path(os.getenv("MS_DATA_DIR") or (self.project_root / "data"))
                        / "operator_audit.jsonl"
                    )
                ).resolve()

                for spec in self.file_specs:
                    source = Path(spec.source_path).resolve()
                    if not source.exists():
                        if spec.required:
                            raise BackupError(
                                f"Required backup source does not exist: {source}"
                            )
                        continue
                    if not source.is_file():
                        if spec.required:
                            raise BackupError(
                                f"Required backup source is not a file: {source}"
                            )
                        continue
                    restore_path = _safe_relative_path(spec.restore_path)
                    staged = staging_path / "files" / Path(restore_path)
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    audit_verification = None
                    if source == operator_audit_source:
                        from core.operator_audit import OperatorAuditLedger

                        audit_verification = OperatorAuditLedger(
                            path=source,
                            required=True,
                        ).copy_verified_snapshot(staged)
                    else:
                        shutil.copy2(source, staged)
                    if staged.suffix.lower() == ".json":
                        try:
                            with staged.open("r", encoding="utf-8") as handle:
                                json.load(handle)
                        except (OSError, json.JSONDecodeError) as exc:
                            if spec.required:
                                raise BackupError(
                                    f"Required JSON source is invalid: {source}: {exc}"
                                ) from exc
                            logger.warning("Skipping invalid optional JSON backup source: %s", source)
                            continue
                    archive_path = f"payload/files/{restore_path}"
                    entry = self._entry_for_path(
                        archive_path=archive_path,
                        restore_path=restore_path,
                        source=staged,
                        kind="json" if staged.suffix.lower() == ".json" else "ledger",
                        required=spec.required,
                    )
                    if audit_verification is not None:
                        entry["content_verification"] = {
                            "type": "operator_hmac_chain",
                            "record_count": int(
                                audit_verification.get("record_count", 0) or 0
                            ),
                            "last_hash": audit_verification.get("last_hash"),
                            "key_id": audit_verification.get("key_id"),
                            "verification_key_count": int(
                                audit_verification.get("verification_key_count", 0)
                                or 0
                            ),
                        }
                    entries.append(entry)
                    staged_files.append((staged, archive_path))

                manifest = {
                    "schema_version": BACKUP_SCHEMA_VERSION,
                    "snapshot_id": snapshot_id,
                    "created_at": _iso_utc(created_at),
                    "label": suffix or None,
                    "database_integrity": sqlite_check,
                    "entry_count": len(entries),
                    "entries": entries,
                }
                manifest_path = staging_path / "manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                with zipfile.ZipFile(
                    partial_path,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                    allowZip64=True,
                ) as archive:
                    archive.write(manifest_path, arcname="manifest.json")
                    for source, archive_path in staged_files:
                        archive.write(source, arcname=_safe_relative_path(archive_path))

                os.replace(partial_path, final_path)
                verification = self.verify_snapshot(final_path)
                retention = self.prune_retention(
                    keep=[final_path, *(retention_keep or [])]
                )
                return {
                    "status": "created",
                    "path": str(final_path),
                    "name": final_path.name,
                    "snapshot_id": snapshot_id,
                    "created_at": manifest["created_at"],
                    "entry_count": len(entries),
                    "archive_size": final_path.stat().st_size,
                    "verified": bool(verification.get("verified")),
                    "retention": retention,
                }
            except Exception:
                for candidate in (partial_path, final_path):
                    try:
                        if candidate.exists():
                            candidate.unlink()
                    except OSError:
                        pass
                raise
            finally:
                if staging_path.exists():
                    shutil.rmtree(staging_path)

    def _read_manifest(self, archive: zipfile.ZipFile) -> Dict[str, Any]:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise BackupVerificationError("Archive contains duplicate member names.")
        if "manifest.json" not in names:
            raise BackupVerificationError("Archive is missing manifest.json.")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
            raise BackupVerificationError(f"Invalid backup manifest: {exc}") from exc
        if not isinstance(manifest, dict):
            raise BackupVerificationError("Backup manifest must be an object.")
        if int(manifest.get("schema_version", 0) or 0) != BACKUP_SCHEMA_VERSION:
            raise BackupVerificationError("Unsupported backup schema version.")
        entries = manifest.get("entries")
        if not isinstance(entries, list) or not entries:
            raise BackupVerificationError("Backup manifest has no entries.")
        if int(manifest.get("entry_count", -1)) != len(entries):
            raise BackupVerificationError("Backup manifest entry count mismatch.")
        return manifest

    def verify_snapshot(self, path: Path | str) -> Dict[str, Any]:
        archive_path = Path(path).resolve()
        if not archive_path.exists() or not archive_path.is_file():
            raise BackupVerificationError(f"Backup archive does not exist: {archive_path}")
        try:
            archive_path.relative_to(self.backup_dir)
        except ValueError as exc:
            raise BackupVerificationError(
                "Backup archive must be inside the configured backup directory."
            ) from exc

        verify_dir = Path(
            tempfile.mkdtemp(prefix=".verify-", dir=str(self.backup_dir))
        ).resolve()
        try:
            verify_dir.relative_to(self.backup_dir)
            with zipfile.ZipFile(archive_path, mode="r") as archive:
                manifest = self._read_manifest(archive)
                names = set(archive.namelist())
                sqlite_entry = None
                operator_audit_entry = None
                seen_members = set()
                seen_restore_paths = set()
                for entry in manifest["entries"]:
                    if not isinstance(entry, dict):
                        raise BackupVerificationError("Invalid backup entry metadata.")
                    member = _safe_relative_path(entry.get("archive_path"))
                    restore_path = _safe_relative_path(entry.get("restore_path"))
                    if member in seen_members:
                        raise BackupVerificationError(
                            f"Manifest references an archive member more than once: {member}"
                        )
                    if restore_path.casefold() in seen_restore_paths:
                        raise BackupVerificationError(
                            f"Manifest contains a duplicate restore path: {restore_path}"
                        )
                    seen_members.add(member)
                    seen_restore_paths.add(restore_path.casefold())
                    if member not in names:
                        raise BackupVerificationError(f"Archive member is missing: {member}")
                    expected_size = int(entry.get("size", -1))
                    expected_digest = str(entry.get("sha256") or "")
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(member, "r") as source:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                            size += len(chunk)
                    if size != expected_size:
                        raise BackupVerificationError(
                            f"Size mismatch for {member}: {size} != {expected_size}"
                        )
                    if not hmac_compare(digest.hexdigest(), expected_digest):
                        raise BackupVerificationError(f"SHA-256 mismatch for {member}")
                    if entry.get("kind") == "sqlite":
                        if sqlite_entry is not None:
                            raise BackupVerificationError(
                                "Backup contains more than one SQLite source."
                            )
                        sqlite_entry = entry
                    content_verification = entry.get("content_verification")
                    if isinstance(content_verification, dict) and (
                        content_verification.get("type") == "operator_hmac_chain"
                    ):
                        if operator_audit_entry is not None:
                            raise BackupVerificationError(
                                "Backup contains more than one operator audit ledger."
                            )
                        operator_audit_entry = entry

                if sqlite_entry is None:
                    raise BackupVerificationError("Backup contains no SQLite source.")
                sqlite_member = _safe_relative_path(sqlite_entry["archive_path"])
                extracted_db = verify_dir / "database.sqlite"
                with archive.open(sqlite_member, "r") as source, extracted_db.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                sqlite_check = _sqlite_integrity(extracted_db)
                operator_audit_check = None
                if operator_audit_entry is not None:
                    from core.operator_audit import OperatorAuditLedger

                    audit_member = _safe_relative_path(
                        operator_audit_entry["archive_path"]
                    )
                    extracted_audit = verify_dir / "operator_audit.jsonl"
                    with archive.open(audit_member, "r") as source, extracted_audit.open(
                        "wb"
                    ) as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    operator_audit_check = OperatorAuditLedger(
                        path=extracted_audit,
                        required=True,
                    ).verify()
                    if not operator_audit_check.get("verified"):
                        raise BackupVerificationError(
                            "Operator audit verification failed: "
                            f"{operator_audit_check.get('error') or 'unknown error'}"
                        )
                    expected_audit = operator_audit_entry["content_verification"]
                    if (
                        int(operator_audit_check.get("record_count", -1))
                        != int(expected_audit.get("record_count", -2))
                        or not hmac_compare(
                            str(operator_audit_check.get("last_hash") or ""),
                            str(expected_audit.get("last_hash") or ""),
                        )
                    ):
                        raise BackupVerificationError(
                            "Operator audit manifest metadata mismatch."
                        )

            return {
                "verified": True,
                "path": str(archive_path),
                "name": archive_path.name,
                "snapshot_id": manifest.get("snapshot_id"),
                "created_at": manifest.get("created_at"),
                "label": manifest.get("label"),
                "entry_count": len(manifest["entries"]),
                "archive_size": archive_path.stat().st_size,
                "sqlite_integrity": sqlite_check,
                "operator_audit_integrity": operator_audit_check,
                "manifest": manifest,
            }
        except (zipfile.BadZipFile, OSError) as exc:
            raise BackupVerificationError(f"Invalid backup archive: {exc}") from exc
        finally:
            if verify_dir.exists():
                shutil.rmtree(verify_dir)

    def list_snapshots(self, *, verify: bool = False) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for path in sorted(
            self.backup_dir.glob(f"{BACKUP_PREFIX}*.zip"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            try:
                if verify:
                    details = self.verify_snapshot(path)
                    details.pop("manifest", None)
                else:
                    with zipfile.ZipFile(path, mode="r") as archive:
                        manifest = self._read_manifest(archive)
                    details = {
                        "verified": None,
                        "path": str(path),
                        "name": path.name,
                        "snapshot_id": manifest.get("snapshot_id"),
                        "created_at": manifest.get("created_at"),
                        "label": manifest.get("label"),
                        "entry_count": len(manifest.get("entries") or []),
                        "archive_size": path.stat().st_size,
                    }
                rows.append(details)
            except BackupError as exc:
                rows.append(
                    {
                        "verified": False,
                        "path": str(path),
                        "name": path.name,
                        "archive_size": path.stat().st_size,
                        "error": str(exc),
                    }
                )
            except (OSError, zipfile.BadZipFile) as exc:
                rows.append(
                    {
                        "verified": False,
                        "path": str(path),
                        "name": path.name,
                        "error": str(exc),
                    }
                )
        return rows

    def prune_retention(
        self,
        *,
        keep: Optional[Sequence[Path | str]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            archives = sorted(
                self.backup_dir.glob(f"{BACKUP_PREFIX}*.zip"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            keep_resolved = {
                Path(path).resolve()
                for path in (keep or [])
            }
            cutoff = time.time() - (self.retention_days * 86_400)
            removed: List[str] = []
            for index, path in enumerate(archives):
                resolved = path.resolve()
                if resolved in keep_resolved:
                    continue
                should_remove = index >= self.retention_count or path.stat().st_mtime < cutoff
                if not should_remove:
                    continue
                try:
                    resolved.relative_to(self.backup_dir)
                    resolved.unlink()
                    removed.append(path.name)
                except OSError as exc:
                    logger.warning("Backup retention could not remove %s: %s", path, exc)
            return {
                "retention_count": self.retention_count,
                "retention_days": self.retention_days,
                "removed": removed,
            }

    def restore_snapshot(
        self,
        path: Path | str,
        *,
        target_root: Optional[Path] = None,
        apply: bool = False,
    ) -> Dict[str, Any]:
        verification = self.verify_snapshot(path)
        manifest = verification["manifest"]
        destination_root = Path(target_root or self.project_root).resolve()
        plan = []
        for entry in manifest["entries"]:
            restore_path = _safe_relative_path(entry["restore_path"])
            destination = (destination_root / Path(restore_path)).resolve()
            try:
                destination.relative_to(destination_root)
            except ValueError as exc:
                raise RestoreSafetyError(
                    f"Restore target escapes destination root: {restore_path}"
                ) from exc
            plan.append(
                {
                    "restore_path": restore_path,
                    "destination": str(destination),
                    "kind": entry.get("kind"),
                    "size": int(entry.get("size", 0) or 0),
                    "sha256": entry.get("sha256"),
                }
            )

        if not apply:
            return {
                "status": "dry_run",
                "verified": True,
                "snapshot_id": manifest.get("snapshot_id"),
                "target_root": str(destination_root),
                "files": plan,
            }
        if not self.allow_restore:
            raise RestoreSafetyError(
                "Restore apply is disabled. Use the guarded CLI with explicit acknowledgement."
            )

        staging = Path(
            tempfile.mkdtemp(prefix=".restore-", dir=str(self.backup_dir))
        ).resolve()
        try:
            staging.relative_to(self.backup_dir)
            archive_path = Path(path).resolve()
            with zipfile.ZipFile(archive_path, mode="r") as archive:
                entry_by_restore = {
                    _safe_relative_path(entry["restore_path"]): entry
                    for entry in manifest["entries"]
                }
                staged_by_restore: Dict[str, Path] = {}
                for restore_path, entry in entry_by_restore.items():
                    member = _safe_relative_path(entry["archive_path"])
                    staged = staging / Path(restore_path)
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member, "r") as source, staged.open("wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    staged_by_restore[restore_path] = staged

            ordered = sorted(
                plan,
                key=lambda row: 1 if row["kind"] == "sqlite" else 0,
            )
            restored: List[str] = []
            for item in ordered:
                restore_path = item["restore_path"]
                destination = Path(item["destination"])
                source = staged_by_restore[restore_path]
                destination.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(
                    prefix=f".{destination.name}.restore-",
                    dir=str(destination.parent),
                )
                temp_path = Path(temp_name)
                try:
                    with os.fdopen(fd, "wb") as target, source.open("rb") as source_handle:
                        shutil.copyfileobj(source_handle, target, length=1024 * 1024)
                        target.flush()
                        os.fsync(target.fileno())
                    if item["kind"] == "sqlite":
                        for suffix in ("-wal", "-shm"):
                            sidecar = Path(f"{destination}{suffix}")
                            if sidecar.exists():
                                sidecar.unlink()
                    os.replace(temp_path, destination)
                finally:
                    if temp_path.exists():
                        temp_path.unlink()
                digest, size = _sha256_path(destination)
                if size != int(item["size"]) or not hmac_compare(digest, str(item["sha256"])):
                    raise BackupVerificationError(
                        f"Restored file verification failed: {restore_path}"
                    )
                if item["kind"] == "sqlite":
                    _sqlite_integrity(destination)
                restored.append(restore_path)
            return {
                "status": "restored",
                "verified": True,
                "snapshot_id": manifest.get("snapshot_id"),
                "target_root": str(destination_root),
                "restored": restored,
            }
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def hmac_compare(left: str, right: str) -> bool:
    """Constant-time digest comparison without importing the auth stack."""
    import hmac

    return hmac.compare_digest(str(left), str(right))


class BackupScheduler:
    """Low-frequency scheduler that maintains verified recovery points."""

    def __init__(self, manager: Optional[BackupManager] = None):
        self.manager = manager or BackupManager()
        self.enabled = _truthy(
            os.getenv("MS_BACKUP_ENABLED"),
            default=not _truthy(os.getenv("TEST_MODE")) and not _truthy(os.getenv("MS_TEST_MODE")),
        )
        self.interval_seconds = _bounded_int(
            os.getenv("MS_BACKUP_INTERVAL_SECONDS", "86400"),
            default=86400,
            minimum=900,
            maximum=31_536_000,
        )
        self.startup_delay_seconds = _bounded_int(
            os.getenv("MS_BACKUP_STARTUP_DELAY_SECONDS", "120"),
            default=120,
            minimum=0,
            maximum=86_400,
        )
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._started_at: Optional[float] = None
        self._last_attempt: Optional[str] = None
        self._last_success: Optional[str] = None
        self._last_error: Optional[str] = None

    def start(self) -> bool:
        with self._lock:
            if not self.enabled:
                return False
            if self._thread and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._started_at = time.time()
            self._thread = threading.Thread(
                target=self._loop,
                daemon=True,
                name="DisasterRecoveryBackup",
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5.0)

    def _latest_age_seconds(self) -> Optional[float]:
        snapshots = self.manager.list_snapshots(verify=False)
        for snapshot in snapshots:
            created_at = snapshot.get("created_at")
            if not created_at:
                continue
            try:
                parsed = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                return max(0.0, (_utc_now() - parsed.astimezone(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                continue
        return None

    def _loop(self) -> None:
        if self._stop_event.wait(self.startup_delay_seconds):
            return
        while not self._stop_event.is_set():
            age = self._latest_age_seconds()
            if age is None or age >= self.interval_seconds:
                self._last_attempt = _iso_utc()
                try:
                    result = self.manager.create_snapshot(label="scheduled")
                    self._last_success = str(result.get("created_at") or _iso_utc())
                    self._last_error = None
                except Exception as exc:
                    self._last_error = str(exc)
                    logger.exception("Scheduled disaster-recovery backup failed")
            wait_for = self.interval_seconds
            age = self._latest_age_seconds()
            if age is not None:
                wait_for = max(60.0, self.interval_seconds - age)
            if self._stop_event.wait(wait_for):
                return

    def status(self) -> Dict[str, Any]:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            latest = self.manager.list_snapshots(verify=False)
            return {
                "enabled": self.enabled,
                "running": running,
                "started_at": (
                    datetime.fromtimestamp(self._started_at, timezone.utc).isoformat()
                    if self._started_at
                    else None
                ),
                "last_attempt": self._last_attempt,
                "last_success": self._last_success,
                "last_error": self._last_error,
                "interval_seconds": self.interval_seconds,
                "startup_delay_seconds": self.startup_delay_seconds,
                "backup_dir": str(self.manager.backup_dir),
                "snapshot_count": len(latest),
                "latest_snapshot": latest[0] if latest else None,
            }


_BACKUP_SCHEDULER: Optional[BackupScheduler] = None
_BACKUP_SCHEDULER_LOCK = threading.Lock()


def get_backup_scheduler() -> BackupScheduler:
    global _BACKUP_SCHEDULER
    with _BACKUP_SCHEDULER_LOCK:
        if _BACKUP_SCHEDULER is None:
            _BACKUP_SCHEDULER = BackupScheduler()
        return _BACKUP_SCHEDULER
