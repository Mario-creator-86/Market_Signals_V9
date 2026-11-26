"""Phase 15 §82/§83/§84 — the goal-hacking diff guard.

Every autonomously produced diff passes `review_diff` BEFORE it can be
applied to an experiment worktree. Protected files (§82), constitutional
golden tests (§84) and evaluation-cost transparency (§83) are enforced on
the diff text itself — fail-closed: an unparseable diff is rejected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

# §82 — autonomous agents may NOT modify these without explicit owner action.
PROTECTED_PATHS = (
    "docs/V9_RISK_CONSTITUTION.md",
    "core/autonomy.py",
    "core/strategy_registry/policy.py",
    "core/backtest/splits.py",
    "risk_constitution_profiles.json",
    "docs/V9_OPEN_SOURCE_MANIFEST.md",
)

# §84 — constitutional tests are immutable for autonomous development.
GOLDEN_TEST_GLOBS = ("tests/golden/",)

# §83 — evaluation-transparency terms; removals from these modules are flagged.
COST_SENSITIVE_MODULES = (
    "core/backtest/",
    "core/trading_costs",
    "core/portfolio/accounting",
)

_HUNK_HEADER = re.compile(r"^@@")
_FILE_HEADER = re.compile(r"^\+\+\+ b/(.+)$")
_SKIP_MARKERS = re.compile(r"pytest\.mark\.(skip|xfail)|pytest\.skip\(|@unittest\.skip")
_COST_REMOVAL = re.compile(r"^(slippage|fees|commission|cost|costs)([\s_=:]|$)", re.IGNORECASE)


class DiffRejected(ValueError):
    """The diff violates §82/§83/§84 and may not enter a worktree."""

    def __init__(self, findings: List["PolicyFinding"]):
        self.findings = list(findings)
        super().__init__("; ".join(f"{f.rule}:{f.path}" for f in self.findings))


@dataclass(frozen=True)
class PolicyFinding:
    rule: str        # PROTECTED_FILE | GOLDEN_TEST_MODIFIED | TEST_WEAKENED | EVALUATION_WEAKENED
    path: str
    detail: str = ""


def _parse_files(diff_text: str):
    """Yield (path, [lines]) per file section; deletions carry '-' lines."""
    current = None
    files: List[List] = []
    for line in str(diff_text or "").splitlines():
        match = _FILE_HEADER.match(line)
        if match:
            current = {"path": match.group(1).strip(), "removed": [], "added": []}
            files.append(current)
            continue
        if current is None:
            continue
        if _HUNK_HEADER.match(line):
            continue
        if line.startswith("--- a/"):
            continue
        if line.startswith("-") and not line.startswith("---"):
            current["removed"].append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            current["added"].append(line[1:])
    return files


def review_diff(diff_text: str) -> List[PolicyFinding]:
    """Return all §82/§83/§84 findings for a unified diff (empty = clean)."""
    findings: List[PolicyFinding] = []
    for section in _parse_files(diff_text):
        path = section["path"]
        if any(path.startswith(prefix) or path == prefix for prefix in PROTECTED_PATHS):
            findings.append(PolicyFinding(
                rule="PROTECTED_FILE", path=path,
                detail="§82 protected surface requires explicit owner action",
            ))
        if any(path.startswith(glob) for glob in GOLDEN_TEST_GLOBS):
            findings.append(PolicyFinding(
                rule="GOLDEN_TEST_MODIFIED", path=path,
                detail="§84 constitutional tests are immutable for autonomous development",
            ))
        if path.startswith("tests/"):
            joined = "\n".join(section["added"])
            if _SKIP_MARKERS.search(joined):
                findings.append(PolicyFinding(
                    rule="TEST_WEAKENED", path=path,
                    detail="§83 adding skip/xfail markers to tests is goal-hacking",
                ))
        if any(path.startswith(prefix) for prefix in COST_SENSITIVE_MODULES):
            for removed in section["removed"]:
                if _COST_REMOVAL.match(removed.strip()):
                    findings.append(PolicyFinding(
                        rule="EVALUATION_WEAKENED", path=path,
                        detail=f"§83 cost parameter removed: {removed.strip()[:60]}",
                    ))
                    break
    return findings


def enforce_diff(diff_text: str) -> None:
    """Raise DiffRejected when the diff violates policy (fail-closed)."""
    findings = review_diff(diff_text)
    if findings:
        raise DiffRejected(findings)


__all__ = [
    "PolicyFinding", "DiffRejected", "review_diff", "enforce_diff",
    "PROTECTED_PATHS", "GOLDEN_TEST_GLOBS", "COST_SENSITIVE_MODULES",
]
