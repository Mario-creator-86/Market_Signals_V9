import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "docs" / "V9_OPEN_SOURCE_MANIFEST.md"
NOTICES = REPO_ROOT / "THIRD_PARTY_NOTICES.md"

EXPECTED_REPOS = [
    "Microsoft Qlib",
    "Microsoft RD-Agent",
    "skfolio",
    "edgartools",
    "Earnings-Volatility-Calculator",
    "FinBERT",
    "MLflow",
    "Optuna",
    "RiskLabAI.py",
    "vectorbt",
    "quant-garage",
    "River",
    "CCXT",
]

PERMISSIVE = ("MIT", "Apache-2.0", "Apache 2.0", "BSD", "ISC", "MPL-2.0", "Unlicense")
INTEGRATION_TYPES = ("INTEGRATE", "ADAPTER", "REFERENCE_ONLY", "REJECT")
FORBIDDEN_TOKENS = ("PENDING", "TBD")


def _rows():
    text = MANIFEST.read_text(encoding="utf-8")
    out = []
    for line in text.splitlines():
        if line.startswith("| ") and "---" not in line and "Repository" not in line:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 10 and cells[0] in EXPECTED_REPOS:
                out.append(cells)
    return out


def test_manifest_file_exists():
    assert MANIFEST.is_file(), "docs/V9_OPEN_SOURCE_MANIFEST.md must exist"


def test_manifest_covers_all_13_repos():
    names = {r[0] for r in _rows()}
    missing = [r for r in EXPECTED_REPOS if r not in names]
    assert not missing, f"manifest missing repos: {missing}"


def test_manifest_has_no_pending_or_tbd():
    bad = []
    for r in _rows():
        for cell in r:
            for tok in FORBIDDEN_TOKENS:
                if cell == tok or tok in cell.split():
                    bad.append((r[0], tok))
                    break
    assert not bad, f"manifest still has PENDING/TBD cells: {bad}"


def test_manifest_decisions_are_valid():
    for r in _rows():
        decision = r[8].upper()
        base = decision.split("(")[0].strip()
        assert base in INTEGRATION_TYPES, f"{r[0]}: bad decision {r[8]!r}"


def test_manifest_integrate_adapter_rows_have_permissive_license_and_pin():
    for r in _rows():
        decision = r[8].upper().split("(")[0].strip()
        if decision in ("INTEGRATE", "ADAPTER"):
            license_cell = r[3]
            commit_cell = r[2]
            assert any(p in license_cell for p in PERMISSIVE), (
                f"{r[0]}: INTEGRATE/ADAPTER requires a verified permissive license, got {license_cell!r}"
            )
            assert commit_cell and commit_cell not in ("TBD", "PENDING", ""), (
                f"{r[0]}: INTEGRATE/ADAPTER requires a pinned release/commit, got {commit_cell!r}"
            )


def test_third_party_notices_exists_and_lists_integrate_adapter_repos():
    assert NOTICES.is_file(), "THIRD_PARTY_NOTICES.md must exist"
    notices_text = NOTICES.read_text(encoding="utf-8")
    for r in _rows():
        decision = r[8].upper().split("(")[0].strip()
        if decision in ("INTEGRATE", "ADAPTER"):
            assert r[0] in notices_text, f"{r[0]} (INTEGRATE/ADAPTER) must appear in THIRD_PARTY_NOTICES.md"