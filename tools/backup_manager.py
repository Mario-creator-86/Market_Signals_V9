"""Operational CLI for verified Market Signals backups and recovery drills."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.disaster_recovery import (  # noqa: E402
    BackupError,
    BackupManager,
)


RESTORE_ACK = "REPLACE_RUNTIME_STATE"


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _manager(args: argparse.Namespace, *, allow_restore: bool = False) -> BackupManager:
    return BackupManager(
        project_root=ROOT,
        database_path=Path(args.database).resolve() if args.database else None,
        backup_dir=Path(args.backup_dir).resolve() if args.backup_dir else None,
        allow_restore=allow_restore,
    )


def _create(args: argparse.Namespace) -> Dict[str, Any]:
    return _manager(args).create_snapshot(label=args.label)


def _list(args: argparse.Namespace) -> Dict[str, Any]:
    manager = _manager(args)
    snapshots = manager.list_snapshots(verify=bool(args.verify))
    return {
        "backup_dir": str(manager.backup_dir),
        "count": len(snapshots),
        "snapshots": snapshots,
    }


def _verify(args: argparse.Namespace) -> Dict[str, Any]:
    result = _manager(args).verify_snapshot(Path(args.archive))
    result.pop("manifest", None)
    return result


def _restore(args: argparse.Namespace) -> Dict[str, Any]:
    apply_restore = bool(args.apply)
    if apply_restore and args.acknowledge != RESTORE_ACK:
        raise BackupError(
            "Restore apply requires --acknowledge REPLACE_RUNTIME_STATE."
        )
    manager = _manager(args, allow_restore=apply_restore)
    archive = Path(args.archive).resolve()
    target_root = Path(args.target_root).resolve() if args.target_root else ROOT
    dry_run = manager.restore_snapshot(
        archive,
        target_root=target_root,
        apply=False,
    )
    if not apply_restore:
        return dry_run

    if target_root == ROOT and str(os.getenv("MS_RESTORE_ALLOWED", "")).strip() != "1":
        raise BackupError(
            "Production-root restore requires MS_RESTORE_ALLOWED=1 after all "
            "API/engine processes have been stopped."
        )

    pre_restore = manager.create_snapshot(
        label="pre-restore",
        retention_keep=[archive],
    )
    restored = manager.restore_snapshot(
        archive,
        target_root=target_root,
        apply=True,
    )
    restored["pre_restore_snapshot"] = pre_restore
    return restored


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, verify, list, or restore cryptographically manifested backups."
    )
    parser.add_argument("--backup-dir", help="Override MS_BACKUP_DIR.")
    parser.add_argument("--database", help="Override MS_SQLITE_DB_PATH.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    create = subcommands.add_parser("create", help="Create and verify a new snapshot.")
    create.add_argument("--label", default="manual")
    create.set_defaults(handler=_create)

    listing = subcommands.add_parser("list", help="List available snapshots.")
    listing.add_argument("--verify", action="store_true")
    listing.set_defaults(handler=_list)

    verify = subcommands.add_parser("verify", help="Verify checksums and SQLite integrity.")
    verify.add_argument("archive")
    verify.set_defaults(handler=_verify)

    restore = subcommands.add_parser(
        "restore",
        help="Plan a restore; applying requires two explicit safety acknowledgements.",
    )
    restore.add_argument("archive")
    restore.add_argument("--target-root")
    restore.add_argument("--apply", action="store_true")
    restore.add_argument("--acknowledge")
    restore.set_defaults(handler=_restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.handler(args)
    except BackupError as exc:
        _print({"status": "error", "error": str(exc)})
        return 2
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
