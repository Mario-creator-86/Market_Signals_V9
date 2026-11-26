"""Verify the tamper-evident operator audit ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.operator_audit import OperatorAuditLedger  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the complete HMAC chain for operator API mutations."
    )
    parser.add_argument(
        "command",
        choices=("status", "verify"),
        help="Read ledger state or force a full cryptographic verification.",
    )
    parser.add_argument("--path", help="Override MS_OPERATOR_AUDIT_PATH.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ledger = OperatorAuditLedger(
        path=Path(args.path).resolve() if args.path else None
    )
    result = ledger.verify() if args.command == "verify" else ledger.status()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "verify":
        return 0 if result.get("verified") else 2
    return 0 if result.get("ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
