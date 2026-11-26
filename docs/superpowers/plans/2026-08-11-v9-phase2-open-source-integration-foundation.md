# Phase 2 — Open-Source Integration Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the §10/§11–§23 open-source evaluation gap — perform the real 13-step evaluation for all 13 repos, fill `docs/V9_OPEN_SOURCE_MANIFEST.md` with finalized decisions + pins, generate `THIRD_PARTY_NOTICES.md` (§24) and pinning records (§25), and guard the manifest's completeness with a contract test — all without merging any new production dependency (integration code lands in consuming phases). Keep the 1645-test green baseline green.

**Architecture:** Phase 2 produces DECISIONS, not integration code. Each repo is inspected (GitHub repo + LICENSE + latest release + Python-3.14 classifiers + maintenance), decided (INTEGRATE | ADAPTER | REFERENCE_ONLY | REJECT), pinned (release/commit + date inspected), and recorded in the 9-field manifest. A contract test asserts the manifest has no `PENDING`/`TBD` rows and every INTEGRATE/ADAPTER row has a pinned release + permissive license. `THIRD_PARTY_NOTICES.md` lists every attribution-bearing dep. V7 untouched; no `pip install` in Phase 2.

**Tech Stack:** Python 3.14.3, pytest (contract test), stdlib (csv/re for manifest parsing if needed). No new deps (§118 — Phase 2 records decisions; consuming phases add the deps).

## Global Constraints

(Copied verbatim from `prompt_to_follow.md`; every task's requirements implicitly include these.)

- **§10:** for every external project — inspect repo, license, latest stable release, Python/runtime versions, dependency conflicts, security, maintenance, exact feature required, compare against V7; choose INTEGRATE | ADAPTER | REFERENCE_ONLY | REJECT; pin version/commit; add tests; record decision. Manifest fields: `repository, URL, commit/release inspected, license, purpose, integration type, files/modules used, dependency impact, decision, reason`.
- **§11–§23:** the 13 repos and their per-repo policies (see design §2 table). §19 RiskLabAI: license MUST be verified before code use; do NOT assume. §20 vectorbt: license reviewed carefully; V9 stays functional if rejected. §21 quant-garage: REFERENCE_ONLY, do NOT copy/vendor/hidden-dependency. §23 CCXT: do NOT replace working V7 providers.
- **§24:** generate + maintain `THIRD_PARTY_NOTICES.md` with every production dependency requiring attribution; keep licenses/notices as legally required.
- **§25:** production deps reproducible; do not depend on latest GitHub `main`; for research repos record repository, commit, release, date inspected.
- **§117:** V7 at `C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium` (HEAD f912a33, 109 dirty) is READ-ONLY — never modify.
- **§116:** no remote push (no remote).
- **§106:** no secrets committed; the edgartools SEC identity is an env var, NOT committed code.
- **§115:** no skip/xfail/`assert True`/delete failing test/weaken assertion.
- **§4:** no forbidden success language ("mostly complete", "should work", "99%", "mock for now", etc.).
- **§5/§7/§23:** no rewrite; evolve V7; do not duplicate working V7 functionality; do not replace working V7 providers.
- **§118:** Phase 2 is the open-source manifest phase; Phase 2 itself merges NO new production dependency (decisions + pins only; consuming phases add deps). The manifest-contract test + any docs are the only code/doc artifacts.
- **Phase 1 baseline:** 1645 passed / 0 failed / exit 0 (commit `f1d65d4`). Phase 2 keeps this green and adds the manifest-contract test(s) only.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `tests/contract/test_open_source_manifest.py` (NEW) | Manifest-completeness contract test. | 1, 5, 6 |
| `docs/V9_OPEN_SOURCE_MANIFEST.md` (MODIFY) | All 13 rows finalized (no PENDING/TBD) + §25 pinning. | 2, 3, 4, 5 |
| `THIRD_PARTY_NOTICES.md` (NEW) | Every attribution-bearing production dep + license/notice. | 5 |
| `docs/V9_ARCHITECTURE.md` (MODIFY, additive) | Open-source integration boundary section. | 6 |
| `docs/V9_IMPLEMENTATION.md` (MODIFY, additive) | Phase 2 record; Phase 2 row → PASS at gate. | 6 |

---

### Task 1: Manifest-contract test (failing first)

**Files:**
- Create: `tests/contract/test_open_source_manifest.py`

**Interfaces:**
- Parses `docs/V9_OPEN_SOURCE_MANIFEST.md` (the markdown table) + checks `THIRD_PARTY_NOTICES.md` exists. Pure-stdlib (read file, split table rows on `|`).

- [ ] **Step 1: Write the failing test**

Create `tests/contract/test_open_source_manifest.py`:

```python
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
    # the manifest table rows start with "| " and are not the header/separator
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
        # decision column is index 8 (Repository,URL,Commit,License,Purpose,Type,Files,Dep,Decision,Reason)
        decision = r[8].upper()
        # accept "ADAPTER (optional)" / "INTEGRATE (likely)" style qualifiers
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/contract/test_open_source_manifest.py -v`
Expected: FAIL — manifest still has `PENDING`/`TBD` rows; `THIRD_PARTY_NOTICES.md` absent.

- [ ] **Step 3: No implementation yet** (the implementation is Tasks 2–5 filling the manifest + notices). Commit the failing test as the TDD anchor.

- [ ] **Step 4: Commit the anchor**

```bash
git add tests/contract/test_open_source_manifest.py
git commit -m "test(phase2): manifest-completeness contract test anchor (failing)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Inspect repos 1–5 (Qlib, RD-Agent, skfolio, edgartools, Earnings-Volatility-Calculator)

**Files:** none (inspection; findings recorded into the manifest in Task 5).

**For each repo, WebFetch the GitHub repo page and extract:** latest default-branch commit SHA (short) + last-commit date, latest stable release tag + date, `LICENSE` (name + SPDX), Python version classifiers (`Programming Language :: Python :: 3.x`) — specifically whether 3.14 is declared, maintenance signal (last commit recency), and the narrow V9 feature required vs V7's existing functionality. Record findings in a scratch note (the manifest is filled in Task 5).

- [ ] **Step 1: Inspect Qlib** — WebFetch `https://github.com/microsoft/qlib` (repo page: latest release, last commit, license) + `https://github.com/microsoft/qlib/blob/main/LICENSE` (license text). Extract: license, latest release, Python classifiers (3.14?), maintenance. Decision per design: **ADAPTER** (consuming phase 6/14; V9 data model authoritative).

- [ ] **Step 2: Inspect RD-Agent** — WebFetch `https://github.com/microsoft/RD-Agent` + its LICENSE. Decision per design: **REFERENCE_ONLY** in Phase 2 (research/autonomous-coding server; needs Docker/WSL/isolated Linux; production stays operational if offline; integration → Phase 14/15).

- [ ] **Step 3: Inspect skfolio** — WebFetch `https://github.com/skfolio/skfolio` + LICENSE. Decision per design: **INTEGRATE** (Phase 9; verify permissive license + 3.14 + sklearn compat).

- [ ] **Step 4: Inspect edgartools** — WebFetch `https://github.com/dgunning/edgartools` + LICENSE. Decision per design: **INTEGRATE** (Phase 10; SEC identity = env var, §106).

- [ ] **Step 5: Inspect Earnings-Volatility-Calculator** — WebFetch `https://github.com/Acelogic/Earnings-Volatility-Calculator` + LICENSE. Decision per design: **REFERENCE_ONLY** (§15 methodology only; do NOT import Tkinter/scraping).

- [ ] **Step 6: Record findings** (scratch — carried into Task 5). No commit (no file change yet).

---

### Task 3: Inspect repos 6–9 (FinBERT, MLflow, Optuna, RiskLabAI)

- [ ] **Step 1: Inspect FinBERT** — WebFetch `https://github.com/ProsusAI/finBERT` + LICENSE. **Also inspect the model-weights terms** (the repo LICENSE covers code; the FinBERT *model* terms are separate — record both). Decision per design: **ADAPTER** (modern Transformers pipeline, §16; do NOT use `pytorch_pretrained_bert`; torch is the heavy dep → Phase 10).

- [ ] **Step 2: Inspect MLflow** — WebFetch `https://github.com/mlflow/mlflow` + LICENSE. Decision per design: **ADAPTER** (Phase 14; V9 keeps own `AIProvider`, §17; MLflow AI Gateway → Phase 3).

- [ ] **Step 3: Inspect Optuna** — WebFetch `https://github.com/optuna/optuna` + LICENSE. Decision per design: **INTEGRATE** (Phase 6/14; §18 research-data-only / never-final-holdout / multi-objective / trial-budget rules recorded as constraints).

- [ ] **Step 4: Inspect RiskLabAI.py (§19 gate)** — WebFetch `https://github.com/risklabai/RiskLabAI.py` + LICENSE. **Determine the license explicitly.** §19: do NOT assume; if incompatible/unclear → REFERENCE_ONLY (published methodology + independent V9 implementation + tests in Phase 7). If clean permissive → ADAPTER (Phase 7). Record the license verbatim.

- [ ] **Step 5: Record findings.** No commit.

---

### Task 4: Inspect repos 10–13 (vectorbt, quant-garage, River, CCXT)

- [ ] **Step 1: Inspect vectorbt (§20 gate)** — WebFetch `https://github.com/polakowo/vectorbt` + LICENSE. **Determine the license explicitly** (vectorbt has had license questions — this is the gate). §20: if license unclear/incompatible → REJECT (V9 stays functional via `InternalBacktestEngine` in Phase 7). If clean permissive → ADAPTER (optional, Phase 7; `BacktestEngine` interface). Record license verbatim + the ray/dask heavy-dep impact.

- [ ] **Step 2: Inspect quant-garage** — WebFetch `https://github.com/rgourley/quant-garage` + LICENSE. Decision per design: **REFERENCE_ONLY** (§21; do NOT copy/vendor/hidden-dependency; concepts only).

- [ ] **Step 3: Inspect River** — WebFetch `https://github.com/online-ml/river` + LICENSE. Decision per design: **REFERENCE_ONLY** in Phase 2 → optional ADAPTER in Phase 6 (§22 detection only initially).

- [ ] **Step 4: Inspect CCXT + decide** — WebFetch `https://github.com/ccxt/ccxt` + LICENSE. Per design §23: V7 has working crypto execution (Binance/KuCoin REST); do NOT replace working V7 providers. Decision: **REJECT** with reason "V7 crypto execution sufficient (§23); re-evaluate only if a future phase needs unified multi-exchange execution; subject to Risk Constitution if revisited."

- [ ] **Step 5: Record findings.** No commit.

---

### Task 5: Fill V9_OPEN_SOURCE_MANIFEST.md + generate THIRD_PARTY_NOTICES.md + §25 pinning

**Files:**
- Modify: `docs/V9_OPEN_SOURCE_MANIFEST.md` (replace all 13 PENDING/TBD rows with finalized rows)
- Create: `THIRD_PARTY_NOTICES.md`

- [ ] **Step 1: Finalize the manifest**

Rewrite `docs/V9_OPEN_SOURCE_MANIFEST.md` so every row has real values (no `PENDING`/`TBD`): `Commit/release` = the inspected tag/SHA + date; `License` = the SPDX name verified from the repo's LICENSE; `Files used` = `none` for REFERENCE_ONLY/REJECT, `none yet (consuming phase)` for ADAPTER/INTEGRATE; `Dep impact` = the verified finding (Python 3.14 status, heavy transitive deps, conflicts); `Decision` = one of INTEGRATE/ADAPTER/REFERENCE_ONLY/REJECT (qualifiers like "(optional)"/"(likely)" may follow in parens); `Reason` = the §10-grounded rationale. Add a `## Dependency pinning (spec §25)` section listing each evaluated repo: repository, commit/release, date inspected (2026-08-11). Keep the §24/§25 reference lines.

- [ ] **Step 2: Generate THIRD_PARTY_NOTICES.md**

Create `THIRD_PARTY_NOTICES.md` listing every production dependency requiring attribution: V7's existing pinned stack (pandas, numpy, yfinance, fastapi, uvicorn, requests, etc. — read from `requirements.txt`) + every repo decided INTEGRATE/ADAPTER in Phase 2. For each: name, URL, license (SPDX), copyright/notice line. REFERENCE_ONLY/REJECT repos are NOT production deps → not listed (note that in a footer).

- [ ] **Step 3: Run the contract test**

Run: `python -m pytest tests/contract/test_open_source_manifest.py -v`
Expected: PASS (all 6 assertions).

- [ ] **Step 4: Commit**

```bash
git add docs/V9_OPEN_SOURCE_MANIFEST.md THIRD_PARTY_NOTICES.md
git commit -m "feat(phase2): filled open-source manifest (13 repos) + third-party notices

All 13 §11-§23 repos evaluated per §10: real commit/release + verified license
+ Python-3.14 compat + dep impact + decision (INTEGRATE/ADAPTER/REFERENCE_ONLY/
REJECT) + reason. THIRD_PARTY_NOTICES.md (§24). Pinning (§25). No new merged
deps (§118) — INTEGRATE/ADAPTER integrations land in consuming phases.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Control docs + exit gate → Phase 2 PASS

**Files:**
- Modify: `docs/V9_ARCHITECTURE.md` (additive: open-source integration boundary section)
- Modify: `docs/V9_IMPLEMENTATION.md` (Phase 2 record; Phase 2 row → PASS)

- [ ] **Step 1: V9_ARCHITECTURE.md** — add an "Open-source integration boundary (Phase 2)" section: V9 evaluates external repos per §10; decisions recorded in `V9_OPEN_SOURCE_MANIFEST.md`; INTEGRATE/ADAPTER repos land in consuming phases behind V9-owned interfaces (V9 data model / `AIProvider` / `BacktestEngine` authoritative); REFERENCE_ONLY = methodology only, no code/vendor/hidden-dep; REJECT = V9 stays functional without it. Link the manifest + notices.

- [ ] **Step 2: V9_IMPLEMENTATION.md** — add the Phase 2 record (Status PASS at gate; delivered = manifest + notices + contract test; files; V7 untouched; no new deps; tests executed; exact results). Flip the **Phase 2 row** in the phase table to **PASS**.

- [ ] **Step 3: Gate — V7 untouched + no new deps + sweeps + regression**

Run:
```bash
git -C "C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium" rev-parse HEAD   # f912a33
git diff f1d65d4..HEAD -- requirements.txt requirements-dev.txt pyproject.toml         # empty
grep -rniE "mostly complete|approximately complete|should work|probably works|99%|mock for now|temporary workaround|untested but implemented" docs/V9_OPEN_SOURCE_MANIFEST.md THIRD_PARTY_NOTICES.md tests/contract/test_open_source_manifest.py   # 0 matches
python -m pytest tests/contract tests/unit tests/integration tests/golden tests/perf -q  # 1645 + new contract tests, 0 failed
```
Expected: V7 = f912a33; deps diff empty; §4 sweep 0 matches; regression 1645 + new (manifest-contract) passed / 0 failed / exit 0.

- [ ] **Step 4: Commit the gate**

```bash
git add docs/V9_ARCHITECTURE.md docs/V9_IMPLEMENTATION.md
git commit -m "test(phase2): exit gate PASS — open-source manifest complete

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:** §10 (13-step eval + 9-field manifest) → Task 5 + manifest; §11–§23 (13 repos) → Tasks 2–4 inspections + Task 5 decisions; §24 (THIRD_PARTY_NOTICES.md) → Task 5; §25 (pinning) → Task 5; §118 (Phase 2 = manifest phase, no merged deps) → the whole phase; §5/§7/§23 (no V7 duplication, CCXT reject) → Task 4 + manifest reasons; §106 (SEC identity env-not-secret) → manifest reason; §115/§117/§4 → verified at gate. ✓
**2. Placeholder scan:** no TBD/TODO in steps; the manifest's `TBD`/`PENDING` are the Phase-0 stubs Task 5 replaces (the contract test asserts they're gone). The inspection tasks describe fetch targets (runtime data, not placeholders). Clean.
**3. Type consistency:** the contract test's column indices (decision = 8, license = 3, commit = 2) match the manifest's 10-column row order `Repository | URL | Commit/release | License | Purpose | Integration type | Files used | Dep impact | Decision | Reason`. Task 5 must preserve that exact column order. ✓
**4. Ambiguity check:** "dependency experiments" = real inspections recorded in the manifest (not merged code); "add tests" (§10) = the manifest-contract test; CCXT = REJECT (concrete §23 reason); Phase 2 merges no deps (all INTEGRATE/ADAPTER code in consuming phases). ✓

No issues found. Plan is complete.