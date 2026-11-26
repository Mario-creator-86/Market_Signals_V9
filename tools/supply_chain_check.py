"""Fail-closed dependency audit and CycloneDX SBOM generation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REQUIREMENTS = (ROOT / "requirements.txt", ROOT / "requirements-dev.txt")
DEFAULT_OUTPUT_DIR = ROOT / "output" / "supply-chain"
BANNED_PACKAGES = {"multipart"}


def _requirement_lines(path: Path) -> Iterable[tuple[int, str]]:
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.split(" #", 1)[0].strip()
        if line and not line.startswith("#"):
            yield line_number, line


def validate_requirement_files(paths: Iterable[Path]) -> list[str]:
    """Require immutable, index-hosted direct dependency declarations."""
    issues: list[str] = []
    for path in paths:
        if not path.is_file():
            issues.append(f"{path}: file not found")
            continue
        for line_number, line in _requirement_lines(path):
            location = f"{path.name}:{line_number}"
            if line.startswith(("-", ".")) or "://" in line or " @ " in line:
                issues.append(f"{location}: local, nested, VCS, and URL requirements are forbidden")
                continue
            try:
                requirement = Requirement(line)
            except InvalidRequirement as exc:
                issues.append(f"{location}: invalid requirement ({exc})")
                continue
            name = canonicalize_name(requirement.name)
            if name in BANNED_PACKAGES:
                issues.append(f"{location}: banned dependency-confusion package '{name}'")
            specifiers = list(requirement.specifier)
            if (
                len(specifiers) != 1
                or specifiers[0].operator != "=="
                or "*" in specifiers[0].version
            ):
                issues.append(f"{location}: dependency must use one exact == version pin")
    return issues


def _run_pip_audit(
    requirement_files: Iterable[Path],
    *,
    output_format: str,
    output_path: Path,
) -> tuple[int, str]:
    command = [
        sys.executable,
        "-m",
        "pip_audit",
        "--progress-spinner",
        "off",
        "--format",
        output_format,
        "--output",
        str(output_path),
    ]
    for path in requirement_files:
        command.extend(("--requirement", str(path)))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    return completed.returncode, output


def _audit_counts(report_path: Path) -> tuple[int, int]:
    if not report_path.is_file():
        return 0, 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    dependencies = report.get("dependencies") or []
    vulnerability_count = sum(len(item.get("vulns") or []) for item in dependencies)
    return len(dependencies), vulnerability_count


def run(requirement_files: list[Path], output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_path = output_dir / "dependency-audit.json"
    sbom_path = output_dir / "market-signals-runtime.cdx.json"
    summary_path = output_dir / "summary.json"

    policy_issues = validate_requirement_files(requirement_files)
    audit_code = 2
    audit_output = ""
    sbom_code = 2
    sbom_output = ""

    if not policy_issues:
        try:
            audit_code, audit_output = _run_pip_audit(
                requirement_files,
                output_format="json",
                output_path=audit_path,
            )
            sbom_code, sbom_output = _run_pip_audit(
                requirement_files[:1],
                output_format="cyclonedx-json",
                output_path=sbom_path,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            audit_output = f"{type(exc).__name__}: {exc}"
            sbom_output = audit_output

    dependency_count, vulnerability_count = _audit_counts(audit_path)
    status = (
        "PASS"
        if not policy_issues
        and audit_code == 0
        and sbom_code == 0
        and vulnerability_count == 0
        else "FAIL"
    )
    summary = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requirements": [str(path.relative_to(ROOT)) for path in requirement_files],
        "dependency_count": dependency_count,
        "vulnerability_count": vulnerability_count,
        "policy_issues": policy_issues,
        "audit_exit_code": audit_code,
        "audit_output": audit_output,
        "sbom_exit_code": sbom_code,
        "sbom_output": sbom_output,
        "audit_report": str(audit_path.relative_to(ROOT)),
        "runtime_sbom": str(sbom_path.relative_to(ROOT)),
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(
        f"[supply-chain] {status}: {dependency_count} dependencies, "
        f"{vulnerability_count} known vulnerabilities"
    )
    for issue in policy_issues:
        print(f"[supply-chain] policy: {issue}")
    if audit_output:
        print(f"[supply-chain] audit: {audit_output}")
    if sbom_output and sbom_output != audit_output:
        print(f"[supply-chain] sbom: {sbom_output}")
    print(f"[supply-chain] summary: {summary_path}")
    return 0 if status == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate dependency policy, block known CVEs, and emit a runtime SBOM."
    )
    parser.add_argument(
        "--requirements",
        action="append",
        type=Path,
        help="Requirements file to audit; repeat for multiple files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Artifact directory (default: output/supply-chain).",
    )
    args = parser.parse_args()
    requirement_files = [
        path if path.is_absolute() else ROOT / path
        for path in (args.requirements or DEFAULT_REQUIREMENTS)
    ]
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    return run(requirement_files, output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
