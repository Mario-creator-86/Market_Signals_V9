# V9 Phase 0 — V7→V9 Migration, Baseline & Control Documents — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the read-only V7 codebase into the V9 directory as a clean copy, establish the verified V9 baseline by running the real test suite, create all 10 V9 control documents, and mark Phase 0 = PASS — without modifying V7 in any way.

**Architecture:** V7 (`Market Signals V7 - Premium`) is a read-only source of record. A one-time Python migration script copies the codebase + preserved data into V9, excluding audit-flagged junk and secrets. Baseline is then verified by execution (app import + pytest suite + smoke). The 10 V9 control documents are created as honest living skeletons whose structure mirrors `prompt_to_follow.md`. V9 runs its own git history (already initialized at `e998d80`).

**Tech Stack:** Python 3.14.3, FastAPI 0.135.1, vanilla JS SPA, SQLite + JSON dual-write, pytest (markers: unit/integration/contract/golden/perf/smoke/e2e), Git.

## Global Constraints

- **V7 is read-only.** No file in `C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium` may be created, modified, or deleted. Every task's gate includes "V7 HEAD = f912a33, dirty count = 109, unchanged."
- **No secrets in V9.** `config.json` (V7) is excluded; V9 uses `config.example.json` as its config template. Hardcoded API keys found anywhere in V9 are scrubbed to `REDACTED`.
- **No forbidden success language** (spec §4). Documents use the §9 status vocabulary (`NOT STARTED | IN PROGRESS | BLOCKED | FAILED | PASS`); future-phase content is marked "to be populated in Phase N," never "done."
- **Tests are not weakened** (spec §115): no skip/xfail/`assert True`/delete/weaken to get green. If the suite regresses vs the V7 baseline (557 passed), Phase 0 = NOT DONE and the cause is investigated.
- **No remote push** (spec §116). Commits are local checkpoints only.
- **Working directory for all commands:** `C:\Users\<user>\PyCharmMiscProject\Market Signals V9` (the Bash tool resets here). Use absolute V7 paths for reads.
- **Commit identity** already set in V9: `Mario <mario@example.com>`.

---

## File Structure

| Path (in V9) | Responsibility | Created in |
|---|---|---|
| `.gitignore` | Keep junk/secrets out of V9 git | Task 1 |
| `docs/v7-source-baseline.txt` | Frozen V7 git state for the "V7 untouched" gate | Task 1 |
| `scripts/migrate_v7_to_v9.py` | One-time clean-copy migration (V7 read-only → V9) | Task 2 |
| `api/`, `core/`, `agents/`, `tools/`, `scripts/`, `frontend/`, `tests/`, `tests_e2e/`, `data/`, `docs/` | Migrated V7 codebase + preserved data + V7 system docs | Task 2 |
| `config.json` | V9 runtime config (placeholders only, gitignored) | Task 3 |
| `docs/v9-baseline-results.md` | Actual results of the baseline execution runs | Task 4 |
| `docs/V9_IMPLEMENTATION.md` | 20-phase operational source of truth; Phase 0 record | Task 5 |
| `docs/V9_ARCHITECTURE.md` | §26 architecture + UI→API→provider map | Task 6 |
| `docs/V9_TEST_MATRIX.md` | 19 GOLD-* + 28 acceptance areas | Task 7 |
| `docs/V9_OPEN_SOURCE_MANIFEST.md` | 12 repos, decision=PENDING | Task 8 |
| `docs/V9_AI_SYSTEM.md` | §34–§44 AI system structure | Task 9 |
| `docs/V9_DATA_POLICY.md` | §27–§33 data policy | Task 10 |
| `docs/V9_RISK_CONSTITUTION.md` | §47 risk fields, PROTECTED | Task 11 |
| `docs/V9_RESEARCH_FACTORY.md` | §3, §75–§79 research factory | Task 12 |
| `docs/V9_OPERATIONS.md` | §45, §74, §98, §103–§106 ops | Task 13 |
| `docs/V9_CHANGELOG.md` | Phase 0 entry | Task 14 |

---

### Task 1: Migration safety net — .gitignore + V7 baseline snapshot

**Files:**
- Create: `.gitignore`
- Create: `docs/v7-source-baseline.txt`

**Interfaces:**
- Produces: `.gitignore` (used by every later commit to keep junk out); `docs/v7-source-baseline.txt` (the frozen reference every later task's "V7 untouched" gate compares against).

- [ ] **Step 1: Write `.gitignore`**

```gitignore
# Python
__pycache__/
*.pyc
*.pyo
.pytest_cache/

# Junk / temp / logs (audit F3/F4)
*.tmp
*.bak
logs/
output/

# Secrets & runtime state (spec §106)
config.json
config.json.lock
*.env

# DB sidecars
*.db-shm
*.db-wal

# Debug artifacts
*.png
*_srv.log
*.log

# OS
Thumbs.db
nul
```

- [ ] **Step 2: Write `docs/v7-source-baseline.txt` (frozen V7 state for the gate)**

Run this and paste the exact output into the file:

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium" && echo "v7_head=$(git rev-parse --short HEAD)" && echo "v7_dirty=$(git status --short | wc -l)"
```

Expected content (fill in real values — HEAD must be `f912a33`, dirty `109`):
```
v7_head=f912a33
v7_dirty=109
```

- [ ] **Step 3: Commit the safety net**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add .gitignore docs/v7-source-baseline.txt
git commit -m "chore(v9-phase0): gitignore + frozen V7 source baseline"
```

- [ ] **Step 4: Verify V7 untouched**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium"
echo "head=$(git rev-parse --short HEAD) dirty=$(git status --short | wc -l)"
```
Expected: `head=f912a33 dirty=109` — must match `docs/v7-source-baseline.txt`.

---

### Task 2: Write and run the V7→V9 migration script

**Files:**
- Create: `scripts/migrate_v7_to_v9.py`

**Interfaces:**
- Consumes: V7 tree at `C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium` (read-only).
- Produces: migrated V9 codebase (`api/`, `core/`, `agents/`, `tools/`, `scripts/`, `frontend/`, `tests/`, `tests_e2e/`, `data/`, `docs/`) + top-level config/build files. Does NOT touch V7.

- [ ] **Step 1: Create `scripts/migrate_v7_to_v9.py`**

```python
#!/usr/bin/env python3
"""One-time V7 -> V9 migration copy. V7 is READ-ONLY; this writes only into V9.
Idempotent: re-running overwrites migrated files but never touches V7.
Run from anywhere; uses absolute paths."""
import os
import shutil
from pathlib import Path

SRC = Path(r"C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium")
DST = Path(r"C:\Users\<user>\PyCharmMiscProject\Market Signals V9")

INCLUDE_DIRS = ["api", "core", "agents", "tools", "frontend", "tests", "tests_e2e"]
INCLUDE_FILES = [
    "requirements.txt", "requirements-dev.txt", "Makefile", "pytest.ini",
    "conftest.py", "run_server.py", "Dockerfile", "compose.production.yml",
    "config.example.json", "watchlist.json",
]
INCLUDE_DATA = [
    "market_signals.db", "pre_earnings.json", "pre_earnings_ai_cache.json",
    "pre_earnings_feedback.json", "pre_earnings_pulse_history.json",
    "news_backup.json", "trade_decisions.json", "trade_journal.json",
    "strategy_performance_cache.json", "scanner_state.json",
    "trade_radar_state.json", "us_macro_calendar_2026.json",
    "signals_state.json", "state.json", "capital.json",
]

EXCLUDE_DIRS = {"__pycache__", "logs", ".git", "node_modules", ".pytest_cache", "manual"}
EXCLUDE_EXT = {".tmp", ".bak", ".pyc", ".pyo", ".png", ".log"}
EXCLUDE_FILES = {
    "config.json", "config.json.lock", "nul", "server_startup.log",
    "AUDIT_REPORT.md", "MASTER_AUDIT_REPORT.md",
    "AGENTS_STATUS.md", "MARKET_SCANNER_V7_PREMIUM_COMPLETED.flag",
}
# Inside tests/: drop one-off debug scripts that carry hardcoded FMP keys (audit G1).
EXCLUDE_NAME_GLOBS = ("debug_fmp_", "debug_inspect", "debug_market", "debug_quarantine",
                      "debug_yahoo", "debug_hybrid", "repro_500", "reproduce_engine",
                      "run_backtest_gpsusdt", "force_fetch_news", "populate_live_state")


def _excluded_file(name: str) -> bool:
    if name in EXCLUDE_FILES:
        return True
    if Path(name).suffix in EXCLUDE_EXT:
        return True
    return any(name.startswith(g) for g in EXCLUDE_NAME_GLOBS)


def _copy_tree(src: Path, dst: Path) -> int:
    n = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        rel = Path(root).relative_to(src)
        out = dst / rel
        out.mkdir(parents=True, exist_ok=True)
        for f in files:
            if _excluded_file(f):
                continue
            shutil.copy2(Path(root) / f, out / f)
            n += 1
    return n


def main() -> None:
    assert SRC.is_dir(), f"V7 source missing: {SRC}"
    assert DST.is_dir(), f"V9 dest missing: {DST}"
    total = 0
    for d in INCLUDE_DIRS:
        s = SRC / d
        if s.is_dir():
            total += _copy_tree(s, DST / d)
    for f in INCLUDE_FILES:
        s = SRC / f
        if s.is_file():
            shutil.copy2(s, DST / f)
            total += 1
    # data subset (preserve user data, spec §7)
    (DST / "data").mkdir(exist_ok=True)
    for f in INCLUDE_DATA:
        s = SRC / "data" / f
        if s.is_file():
            shutil.copy2(s, DST / "data" / f)
            total += 1
    # yfinance cache dir (regenerable, but small and useful for offline baseline)
    yc = SRC / "data" / "yfinance_cache"
    if yc.is_dir():
        total += _copy_tree(yc, DST / "data" / "yfinance_cache")
    # docs: copy V7 system docs (reference) but NOT V7's own superpowers/plans
    (DST / "docs").mkdir(exist_ok=True)
    for entry in os.listdir(SRC / "docs"):
        if entry in ("superpowers", "plans"):
            continue
        s = SRC / "docs" / entry
        if s.is_file() and not _excluded_file(entry):
            shutil.copy2(s, DST / "docs" / entry)
            total += 1
        elif s.is_dir():
            total += _copy_tree(s, DST / "docs" / entry)
    # V7 audit reports preserved as read-only reference under docs/v7-audit/
    audit_dst = DST / "docs" / "v7-audit"
    audit_dst.mkdir(parents=True, exist_ok=True)
    for rep in ("MASTER_AUDIT_REPORT.md", "AUDIT_REPORT.md"):
        s = SRC / rep
        if s.is_file():
            shutil.copy2(s, audit_dst / rep)
    print(f"migration complete: {total} files copied into {DST}")
    print("V9 top-level:", sorted(p.name for p in DST.iterdir()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the migration**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
python scripts/migrate_v7_to_v9.py
```
Expected: `migration complete: <N> files copied into ...` and a top-level listing that includes `api core agents tools frontend tests tests_e2e docs data scripts`.

- [ ] **Step 3: Verify V9 received the codebase**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
ls api core agents frontend tests tests_e2e data docs && echo "--- key files ---" && ls requirements.txt Makefile run_server.py config.example.json watchlist.json
```
Expected: each directory lists files; key files exist.

- [ ] **Step 4: Verify V7 untouched (gate)**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium"
echo "head=$(git rev-parse --short HEAD) dirty=$(git status --short | wc -l)"
```
Expected: `head=f912a33 dirty=109` — must equal `docs/v7-source-baseline.txt`.

- [ ] **Step 5: Commit the migrated tree**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add -A
git commit -m "feat(v9-phase0): migrate V7 codebase into V9 (clean copy, no secrets/junk)"
```

---

### Task 3: Post-migration hygiene — scrub secrets, create V9 config, verify clean

**Files:**
- Create: `config.json` (from `config.example.json`, gitignored)
- Modify: any migrated file containing a hardcoded API key → scrubbed to `REDACTED`

**Interfaces:**
- Produces: a V9 tree with no hardcoded API keys and a working placeholder `config.json` so `import api.main` (Task 4) builds.

- [ ] **Step 1: Scan V9 for hardcoded API-key patterns**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
grep -rEnE '[0-9a-f]{32}' --include=*.py tests tools scripts api core agents 2>/dev/null | head -50
```
Inspect hits. FMP/Marketaux keys are 32-hex. A few false positives (e.g., md5/hash constants) are OK; genuine API keys must be scrubbed.

- [ ] **Step 2: Scrub any genuine API key to `REDACTED`**

For each confirmed hardcoded key (expected in migrated test/debug files, if any survived the Task-2 exclude), use `Edit` to replace the key literal with `"REDACTED"`. Do not delete the test (spec §115). If a hit is inside a string like `API_KEY = "abcd...32hex"`, replace the 32-hex value only.

- [ ] **Step 3: Confirm V9 has no junk directories**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
echo "logs? $(ls -d logs 2>/dev/null || echo absent)"
echo "tmp? $(find . -name '*.tmp' | head -1 || echo none)"
echo "pycache? $(find . -type d -name __pycache__ | head -1 || echo none)"
echo "v7 .git? $(ls -d .git 2>/dev/null | wc -l) (V9's own .git is expected)"
echo "config.json from v7? $(grep -l 'marketaux' config.json 2>/dev/null || echo 'no v7 config.json')"
```
Expected: `logs? absent`, no `.tmp`, no `__pycache__`, V9 `.git` present (V9's own), no V7 `config.json` with a marketaux key.

- [ ] **Step 4: Create V9 runtime `config.json` from the example (placeholders only)**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
cp config.example.json config.json
```
Then open `config.json` and confirm it contains no real API keys (it is the example template). Leave placeholders. (It is gitignored by Task 1's `.gitignore`.)

- [ ] **Step 5: Commit scrubbed files (config.json stays gitignored)**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add -A
git status --short | head   # confirm config.json is NOT staged
git commit -m "chore(v9-phase0): scrub hardcoded keys; seed placeholder config.json"
```
Expected: `config.json` does not appear in the staged list (gitignored).

---

### Task 4: Establish V9 baseline by execution

**Files:**
- Create: `docs/v9-baseline-results.md`

**Interfaces:**
- Consumes: migrated V9 codebase (Task 2/3) with deps already installed in the system Python 3.14.3 env (V7 used the same).
- Produces: `docs/v9-baseline-results.md` with actual pass/fail counts, consumed by Task 5's `V9_IMPLEMENTATION.md` Phase 0 record.

- [ ] **Step 1: Verify FastAPI app builds**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
python -c "import api.main; print('APP_IMPORT_OK')" 2>&1 | tail -5
```
Expected: prints `APP_IMPORT_OK` (an `MS_API_KEY not set` auth warning is acceptable). Record any error.

- [ ] **Step 2: Run the regression suite (same command as V7 audit §9)**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
python -m pytest tests/unit tests/integration tests/contract tests/golden tests/perf -q 2>&1 | tail -20
```
Expected: ~557 passed, exit 0 (the V7 audit §9 baseline). Record the exact `N passed` and any failures/errors. If failures appear, record them verbatim — do NOT skip/xfail/weaken tests (spec §115); investigate root cause and record as a Phase 0 blocker if not resolved.

- [ ] **Step 3: Run the smoke gate**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
python -m pytest -m "smoke and not e2e" tests/unit tests/integration tests/contract tests/perf -q 2>&1 | tail -10
```
Expected: smoke subset passes. Record exact counts.

- [ ] **Step 4: Write `docs/v9-baseline-results.md` with the real numbers**

Fill in the actual values from Steps 1–3 (no assumed values):

```markdown
# V9 Baseline Results (Phase 0)

**Date:** 2026-08-11
**Source:** migrated copy of V7 @ f912a33, run in V9 directory.
**Python:** 3.14.3

| Check | Command | Result |
|---|---|---|
| App import | `python -c "import api.main"` | APP_IMPORT_OK / <error> |
| Regression suite | `pytest tests/unit tests/integration tests/contract tests/golden tests/perf` | <N> passed, <M> failed, <K> errors (exit <code>) |
| Smoke | `pytest -m "smoke and not e2e" ...` | <N> passed, <M> deselected (exit <code>) |
| Frontend | static SPA served by FastAPI (no build step) | served / <note> |
| E2E (tests_e2e) | Playwright, Windows host | accepted-skip (RUN_E2E=0) — NOT recorded as PASS |

## Comparison vs V7 audit §9 baseline
V7 §9: 557 passed in 27.46s, exit 0.
V9 this run: <fill>.
Delta: <fill — must be 0 new failures for Phase 0 PASS>.

## V7 known failures (carried from docs/v7-audit/MASTER_AUDIT_REPORT.md, repaired)
All 8 P0 + ~20 P1 from the V7 master audit were repaired; gate green per audit §9. Not re-audited in Phase 0.
```

- [ ] **Step 5: Commit baseline results**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add docs/v9-baseline-results.md
git commit -m "docs(v9-phase0): record verified V9 baseline run results"
```

---

### Task 5: `docs/V9_IMPLEMENTATION.md` — 20-phase tracker + Phase 0 record

**Files:**
- Create: `docs/V9_IMPLEMENTATION.md`

**Interfaces:**
- Consumes: `docs/v9-baseline-results.md` (Task 4), `docs/v7-source-baseline.txt` (Task 1), migration results (Task 2/3).
- Produces: the operational source of truth; Phase 0 = `PASS`. All later phases read their status from here.

- [ ] **Step 1: Write `docs/V9_IMPLEMENTATION.md`**

```markdown
# V9_IMPLEMENTATION — Operational Source of Truth

Market Scanner V9 = direct, tested evolution of V7 (migrated into the V9 directory; V7 is a read-only source of record). Spec basis: `prompt_to_follow.md`.

Allowed statuses (spec §9): `NOT STARTED | IN PROGRESS | BLOCKED | FAILED | PASS`. PASS requires actual execution.

## Phases

| Phase | Title | Status |
|---|---|---|
| 0 | V7 Full Audit (existing V7 audit reused) + V7→V9 migration + baseline + V9 control docs | IN PROGRESS |
| 1 | Data & Architecture Foundation (canonical data, point-in-time, data quality) | NOT STARTED |
| 2 | Open-Source Integration Foundation (manifest evaluations) | NOT STARTED |
| 3 | Multi-Provider AI Platform | NOT STARTED |
| 4 | AI Tool & Decision Engine | NOT STARTED |
| 5 | Pre-Earnings V9 | NOT STARTED |
| 6 | Signal & ML Engine | NOT STARTED |
| 7 | Backtest & Validation Engine | NOT STARTED |
| 8 | Strategy Registry / Champion-Challenger | NOT STARTED |
| 9 | Capital / Portfolio / Risk | NOT STARTED |
| 10 | News / FinBERT / SEC | NOT STARTED |
| 11 | Autonomous Paper Trading | NOT STARTED |
| 12 | AI Trading Cockpit UI | NOT STARTED |
| 13 | Decision Ledger / Model Lab | NOT STARTED |
| 14 | Research Factory | NOT STARTED |
| 15 | Autonomous Development Factory | NOT STARTED |
| 16 | Shadow / Promotion / Rollback | NOT STARTED |
| 17 | Complete Regression / E2E | NOT STARTED |
| 18 | Security / Performance / Hardening | NOT STARTED |
| 19 | Final Acceptance | NOT STARTED |

## Phase 0 — V7→V9 Migration, Baseline & Control Documents

- **Goal:** Migrate read-only V7 into V9 as a clean copy, verify the baseline by execution, create all V9 control documents, mark Phase 0 = PASS, V7 untouched.
- **Status:** IN PROGRESS → PASS (set to PASS by Task 15 gate).
- **Existing V7 components reused:** FastAPI backend (`api/`, `core/`, `agents/`), vanilla JS SPA (`frontend/`), SQLite + JSON dual-write state, `core/strategy_v2/`, `walk_forward_validation`, `reserved_holdout_validation`, `oos_validation_reports`, `feedback_loop`, `confidence_gates`, `autonomous_control`, `autonomous_trade_guard`, `trade_decision`, `capital_manager`, Ollama client, news + pre-earnings agents, watchlist/capital/signals state, 4 backtesters (to be consolidated in Phase 7).
- **Files inspected:** V7 tree at `Market Signals V7 - Premium` @ `f912a33` (109 dirty files, left untouched); `MASTER_AUDIT_REPORT.md` (8 P0 + ~20 P1 repaired, 557 passing).
- **Files created (in V9):** `.gitignore`, `scripts/migrate_v7_to_v9.py`, migrated `api/ core/ agents/ tools/ frontend/ tests/ tests_e2e/ data/ docs/`, `config.json` (placeholder, gitignored), `docs/v7-source-baseline.txt`, `docs/v9-baseline-results.md`, `docs/V9_*.md` (10 control documents), `docs/v7-audit/` (audit reports preserved as reference).
- **Files modified:** none in V7; only new files in V9.
- **Dependencies:** existing system Python 3.14.3 env (deps from `requirements.txt` already installed for V7); no new deps in Phase 0.
- **Migrations:** none (verbatim code copy; schema unchanged).
- **API changes:** none.
- **UI changes:** none.
- **Tests executed:** `python -c "import api.main"`; `pytest tests/unit tests/integration tests/contract tests/golden tests/perf`; `pytest -m "smoke and not e2e" ...`. (See `docs/v9-baseline-results.md` for exact numbers.)
- **Exact results:** see `docs/v9-baseline-results.md`.
- **Bugs found:** 0 new (V7 known failures already repaired per audit; not re-audited).
- **Bugs fixed:** 0 (Phase 0 makes no code changes).
- **Remaining work:** Phase 1 onward (see phase table).
- **Blockers:** none (or list exact failing items if the gate is not green).
```

- [ ] **Step 2: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add docs/V9_IMPLEMENTATION.md
git commit -m "docs(v9-phase0): V9_IMPLEMENTATION tracker + Phase 0 record"
```

---

### Task 6: `docs/V9_ARCHITECTURE.md`

**Files:**
- Create: `docs/V9_ARCHITECTURE.md`

- [ ] **Step 1: Write `docs/V9_ARCHITECTURE.md`**

````markdown
# V9_ARCHITECTURE

Living document. Structure mandated by spec §26 and §101. Content minimal at Phase 0; populated in Phases 1–12.

## Conceptual architecture (spec §26, adapted to real V7 modules)

```text
DATA PROVIDERS  (yfinance/Yahoo, Binance REST via core/http_utils, FinnHub, RSS, FMP stub)
      │
      ▼
DATA INGESTION   (core/engine.py ~3.8k lines; agents/scanner_agent, premarket_scanner, us_market_news, news_tab, economic_calendar, pre_earnings)
      │
      ▼
POINT-IN-TIME STORE  (TO BE BUILT, Phase 1) — currently SQLite (data/market_signals.db) + JSON dual-write (state.json, signals_state.json, capital.json, watchlist.json)
      │
      ▼
DATA VALIDATION  (core/data_guard.py, core/data_quality.py, core/price_integrity.py) — TO BE EXTENDED, Phase 1
      │
      ▼
FEATURE ENGINE  (core/ta.py, core/market_structure.py, core/volume_structure.py, core/scoring_system.py)
      │
      ├──────────────┬──────────────┐
      ▼              ▼              ▼
PRE-EARNINGS    SIGNALS        NEWS / SEC
(agents/pre_earnings_agent,   (core/signals.py,    (agents/news_tab_agent,
 core/autonomous_candidates)    core/signal_quality) agents/us_market_news_agent,
                                 TO ADD edgartools, Phase 10)
      └──────────────┴──────────────┘
      ▼
STRATEGY ENGINE  (core/strategy_v2/: regime_engine, selector, evaluators, risk_manager, registry, performance_cache)
      │
      ▼
ML ENGINE  (TO BE BUILT around Qlib adapter, Phase 6) — currently core/feedback_loop, core/confidence_gates, core/ticker_learning
      │
      ▼
AI ANALYST  (core/ollama_client, core/ollama_decision) — TO BE REPLACED by AIProvider, Phase 3
      │
      ▼
AI CRITIC  (TO BE BUILT, Phase 4)
      │
      ▼
DECISION ENGINE  (core/trade_decision, core/autonomous_control, core/autonomous_trade_guard, core/capital_entry_guard) — TO BE EXTENDED, Phase 4
      │
      ▼
DETERMINISTIC RISK  (core/risk_limits, core/strategy_v2/risk_manager) — TO BE EXTENDED per Risk Constitution, Phase 9
      │
      ▼
CAPITAL ENGINE  (core/capital_manager, core/position_sizing, core/trading_costs)
      │
      ▼
EXECUTION ENGINE  (ExecutionBroker interface TO BE BUILT, Phase 11) — currently PaperBroker path in capital_manager
      │
      ▼
POSITIONS  (capital.json + SQLite) → POSITION MONITOR (core/exit_manager, core/feedback_loop)
      │
      ▼
DECISION LEDGER  (core/analysis_store, core/trade_audit, core/execution_archive) — TO BE EXTENDED, Phase 13
      │
      ▼
OUTCOME STORE  (core/feedback_loop, core/strategy_v2/performance_cache) — TO BE EXTENDED, Phase 13
      │
      ▼
RESEARCH FACTORY  (TO BE BUILT, Phase 14) → CHALLENGER PIPELINE (Phase 16) → PROMOTE / REJECT (Phase 16)
```

Adapt this to V7. Do not introduce distributed infrastructure unnecessarily (spec §26).

## UI → client → API → service → provider map (spec §101)

| UI tab (frontend/) | client (data_store.js) | API route (api/main.py) | service (api/*_service.py) | provider (core/) |
|---|---|---|---|---|
| Signals | fetch /api/state | /api/state | api/state_service.py | core/engine.py, core/signals.py |
| Watchlist | fetch /api/watchlist | /api/watchlist* | api/state_service.py | core/state_persistence |
| PreMarket | fetch /api/premarket | /api/premarket | api/state_service.py | agents/premarket_scanner_agent |
| PreEarnings | fetch /api/pre-earnings | /api/pre-earnings* | api/state_service.py | agents/pre_earnings_agent |
| Capital | fetch /api/capital | /api/capital* | api/state_service.py | core/capital_manager |
| News | fetch /api/news-tab/* | /api/news-tab/* | api/state_service.py | agents/news_tab_agent, us_market_news_agent |
| Calendar | fetch /api/calendar | /api/calendar* | api/state_service.py | agents/economic_calendar_agent |
| Agents | fetch /api/agents | /api/agents* | api/agents_service.py | core/agent_manager |
| Settings | fetch /api/config | /api/config* | api/config_service.py | core/config.py |

Unresolved critical routes at Phase 0: none known (V7 audit §4 verified the frontend→backend contract clean: 60+ URLs, zero mismatches). To be re-verified per phase as new routes are added.
````

- [ ] **Step 2: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add docs/V9_ARCHITECTURE.md
git commit -m "docs(v9-phase0): V9_ARCHITECTURE — §26 + UI->API->provider map"
```

---

### Task 7: `docs/V9_TEST_MATRIX.md`

**Files:**
- Create: `docs/V9_TEST_MATRIX.md`

- [ ] **Step 1: Write `docs/V9_TEST_MATRIX.md`**

```markdown
# V9_TEST_MATRIX

Living document. Rows are critical acceptance items; status uses §9 vocabulary. All `NOT STARTED` at Phase 0.

## Golden tests (spec §107)

| ID | Name | Status | Owner | Notes |
|---|---|---|---|---|
| GOLD-001 | No look-ahead | NOT STARTED | | |
| GOLD-002 | Point-in-time AI replay | NOT STARTED | | |
| GOLD-003 | Earnings BMO alignment | NOT STARTED | | |
| GOLD-004 | Earnings AMC alignment | NOT STARTED | | |
| GOLD-005 | Pre-Earnings score | NOT STARTED | | |
| GOLD-006 | Capital conservation | NOT STARTED | | |
| GOLD-007 | Exact trade accounting | NOT STARTED | | |
| GOLD-008 | Risk veto | NOT STARTED | | |
| GOLD-009 | Kill Switch | NOT STARTED | | |
| GOLD-010 | Duplicate order prevention | NOT STARTED | | |
| GOLD-011 | Stale data rejection | NOT STARTED | | |
| GOLD-012 | Prompt injection | NOT STARTED | | |
| GOLD-013 | AI provider fallback | NOT STARTED | | |
| GOLD-014 | All AI unavailable → no new autonomous trade | NOT STARTED | | |
| GOLD-015 | Bad challenger rejected | NOT STARTED | | |
| GOLD-016 | Protected policy modification rejected | NOT STARTED | | |
| GOLD-017 | Rollback | NOT STARTED | | |
| GOLD-018 | DB migration | NOT STARTED | | |
| GOLD-019 | Broker reconciliation | NOT STARTED | | |

## Acceptance areas (spec §121)

| Area | Status | Owner | Notes |
|---|---|---|---|
| SYSTEM | NOT STARTED | | |
| DATA | NOT STARTED | | |
| AI PROVIDERS | NOT STARTED | | |
| AI DECISION ENGINE | NOT STARTED | | |
| PRE-EARNINGS | NOT STARTED | | |
| SIGNALS | NOT STARTED | | |
| ML | NOT STARTED | | |
| BACKTEST | NOT STARTED | | |
| STRATEGIES | NOT STARTED | | |
| CAPITAL | NOT STARTED | | |
| RISK | NOT STARTED | | |
| NEWS | NOT STARTED | | |
| SEC | NOT STARTED | | |
| PAPER EXECUTION | NOT STARTED | | |
| POSITION MANAGEMENT | NOT STARTED | | |
| DECISION LEDGER | NOT STARTED | | |
| MODEL LAB | NOT STARTED | | |
| RESEARCH FACTORY | NOT STARTED | | |
| AUTONOMOUS DEVELOPMENT | NOT STARTED | | |
| SHADOW | NOT STARTED | | |
| ROLLBACK | NOT STARTED | | |
| UI | NOT STARTED | | |
| SECURITY | NOT STARTED | | |
| PERFORMANCE | NOT STARTED | | |
| DATABASE MIGRATION | NOT STARTED | | |
| PRODUCTION BUILD | NOT STARTED | | |
| E2E | NOT STARTED | | |
| GOLDEN TESTS | NOT STARTED | | |

## Starting point (honest)
V7 golden suite is near-empty (2 tests, audit §9). Phase 0 introduces no new tests (no code changes). Golden tests are built in their respective phases (GOLD-001/002 in Phase 1; GOLD-006/007 in Phase 9; GOLD-008/009/016 in Phase 9; GOLD-010/018 in Phase 11; GOLD-012 in Phase 4; GOLD-013/014 in Phase 3; GOLD-015/017/019 in Phase 16). Final acceptance requires all critical rows PASS (spec §122).
```

- [ ] **Step 2: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add docs/V9_TEST_MATRIX.md
git commit -m "docs(v9-phase0): V9_TEST_MATRIX — 19 GOLD-* + 28 acceptance areas"
```

---

### Task 8: `docs/V9_OPEN_SOURCE_MANIFEST.md`

**Files:**
- Create: `docs/V9_OPEN_SOURCE_MANIFEST.md`

- [ ] **Step 1: Write `docs/V9_OPEN_SOURCE_MANIFEST.md`**

```markdown
# V9_OPEN_SOURCE_MANIFEST

Per spec §10, for every external project record: repository, URL, commit/release inspected, license, purpose, integration type (INTEGRATE | ADAPTER | REFERENCE_ONLY | REJECT), files/modules used, dependency impact, decision, reason.

All `decision = PENDING` at Phase 0. License/feature evaluation happens in Phase 2 (spec §118). Nothing is vendored or depended on in Phase 0.

| Repository | URL | Commit/release | License | Purpose | Integration type | Files used | Dep impact | Decision | Reason |
|---|---|---|---|---|---|---|---|---|---|
| Microsoft Qlib | https://github.com/microsoft/qlib | TBD | TBD | ML / quant research pipeline, feature/model research, alpha, training, research backtest | ADAPTER (likely) | none yet | TBD | PENDING | §11: do not make domain logic depend on Qlib directly; V9 data model authoritative |
| Microsoft RD-Agent | https://github.com/microsoft/RD-Agent | TBD | TBD | autonomous hypothesis/factor/model research, researcher-developer separation | TBD | none yet | TBD | PENDING | §12: research only, not direct execution; may need Docker/WSL/isolated worker |
| skfolio | https://github.com/skfolio/skfolio | TBD | TBD | portfolio optimization, risk budgeting, HRP/NCO, CVaR, stress testing, CV | INTEGRATE (likely) | none yet | TBD | PENDING | §13: compare advanced allocation vs equal-weight/inverse-vol baselines |
| edgartools | https://github.com/dgunning/edgartools | TBD | TBD | SEC EDGAR 10-K/10-Q/8-K/Form 4, structured filing extraction | INTEGRATE (likely) | none yet | TBD | PENDING | §14: configure SEC identity, respect access rules, cache responsibly |
| Earnings-Volatility-Calculator | https://github.com/Acelogic/Earnings-Volatility-Calculator | TBD | TBD | earnings vol methodology, IV/RV, IV30/RV30, ATR, Yang-Zhang, term structure | SELECTIVE (methodology) | none yet | none | PENDING | §15: do NOT import Tkinter UI or scraping; use verified formulas + our providers/tests/UI |
| FinBERT | https://github.com/ProsusAI/finBERT | TBD | TBD | financial sentiment | INTEGRATE (via modern transformers) | none yet | TBD | PENDING | §16: do NOT use obsolete pytorch_pretrained_bert; use maintained Transformers pipeline |
| MLflow | https://github.com/mlflow/mlflow | TBD | TBD | experiment tracking, model/prompt registry, AI tracing, evaluation, lineage | ADAPTER (preferred) | none yet | TBD | PENDING | §17: adapter boundary; V9 keeps own AIProvider interface; MLflow AI Gateway optional |
| Optuna | https://github.com/optuna/optuna | TBD | TBD | controlled hyperparameter optimization, pruning, multi-objective | INTEGRATE (likely) | none yet | TBD | PENDING | §18: research only; never optimize final holdout; multi-objective incl OOS return, drawdown, Sharpe, stability, turnover, costs; trial budgets |
| RiskLabAI.py | https://github.com/risklabai/RiskLabAI.py | TBD | TBD | triple barrier, meta-labeling, Purged K-Fold, CPCV, walk-forward, DSR, PBO, HRP/NCO | TBD | none yet | TBD | PENDING | §19: LICENSE MUST BE VERIFIED before any code use; if incompatible, use published methodology + independent implementation + tests |
| vectorbt | https://github.com/polakowo/vectorbt | TBD | TBD | large-scale vectorized backtesting, parameter sweeps | ADAPTER (optional) | none yet | TBD | PENDING | §20: license reviewed before integration; not required for core V9; BacktestEngine interface + InternalBacktestEngine + VectorBTAdapter |
| quant-garage | https://github.com/rgourley/quant-garage | TBD | TBD | earnings drilldown, event studies, position sizing, factor research | REFERENCE_ONLY | none | none | PENDING | §21: do NOT copy/vendor/hidden-dependency; concepts/methodology only |
| River | https://github.com/online-ml/river | TBD | TBD | streaming metrics, online stats, concept-drift detection, incremental models | OPTIONAL | none yet | TBD | PENDING | §22: detection only initially, not automatic model mutation |
| CCXT (optional) | https://github.com/ccxt/ccxt | TBD | TBD | unified crypto exchange execution | TBD | none yet | TBD | PENDING | §23: only if V7 crypto execution insufficient; subject to Risk Constitution |

## THIRD_PARTY_NOTICES.md
To be generated in Phase 2 with every production dependency requiring attribution (spec §24).

## Dependency pinning (spec §25)
Production deps reproducible (V7 requirements.txt already pinned 2026-07-31). Research repos: record repo/commit/release/date-inspected when evaluated in Phase 2.
```

- [ ] **Step 2: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add docs/V9_OPEN_SOURCE_MANIFEST.md
git commit -m "docs(v9-phase0): V9_OPEN_SOURCE_MANIFEST — 12 repos, decision=PENDING"
```

---

### Task 9: `docs/V9_AI_SYSTEM.md`

**Files:**
- Create: `docs/V9_AI_SYSTEM.md`

- [ ] **Step 1: Write `docs/V9_AI_SYSTEM.md`**

```markdown
# V9_AI_SYSTEM

Living document. Structure from spec §34–§44. Implementation status `NOT STARTED` at Phase 0; built in Phases 3–4.

## AIProvider interface (§34)
Normalized capabilities: `list_models()`, `health_check()`, `capabilities()`, `generate()`, `generate_structured()`, `tool_call()`, `stream()`, `usage()`. Domain logic is vendor-independent via this interface (§17). Replaces V7 `core/ollama_client.py` / `core/ollama_decision.py` in Phase 3.

## Providers (§35)
Required: Ollama Local, Ollama Cloud, OpenAI, Anthropic, Google Gemini. Adapter candidates: xAI, DeepSeek, Mistral, OpenRouter. Model names not hardcoded; verify current official APIs at implementation time (Phase 3).

## Model registry (§36)
Fields: provider, model_id, display_name, capabilities, tool_support, structured_output_support, context_metadata, health, enabled, last_checked. Dynamic discovery where supported; manual config otherwise.

## Model assignment roles (§37)
Market Analyst, Pre-Earnings Analyst, News Analyst, Quant Analyst, Portfolio Manager, Risk Critic, Research Scientist, Coding Agent, Code Reviewer, Security Reviewer. One model for all roles allowed.

## Model router (§38)
fast/cheap → initial triage; strong reasoning → deeper analysis; independent model → critic. Do not spend expensive calls on thousands of low-quality candidates.

## AI tool architecture (§39)
AI reasons through registered application tools (allow-listed), e.g.: get_market_snapshot, get_ohlcv, get_technical_features, get_market_regime, get_sector_regime, get_earnings_event, get_historical_earnings, get_earnings_surprises, get_options_analysis, get_pre_earnings_analysis, get_news, get_news_sentiment, get_sec_context, get_signal, get_strategy, get_strategy_performance, compare_strategies, run_backtest, get_backtest_result, get_portfolio, get_position, get_portfolio_risk, get_available_capital, calculate_allowed_position_size, get_decision_history, get_trade_history, propose_order, propose_close.

## AI must NOT have unrestricted access (§40)
No shell, arbitrary Python, SQL, filesystem, arbitrary HTTP, broker credentials, or secrets. Allow-listed domain tools only.

## AI output schema (§41)
Validated JSON, e.g.:
```json
{ "action": "OPEN_LONG", "ticker": "NVDA", "horizon": "SWING", "confidence": 0.78,
  "strategy_id": "momentum_v3", "thesis": "...", "supporting_evidence": [],
  "opposing_evidence": [], "risk_flags": [], "data_quality": 0.94, "requires_more_data": false }
```
Allowed actions: WATCH, NO_TRADE, OPEN_LONG, OPEN_SHORT, HOLD, REDUCE, CLOSE.

## AI validation pipeline (§42)
PARSE → SCHEMA VALIDATION → SEMANTIC VALIDATION → AUTHORITATIVE DATA CHECK → POLICY VALIDATION → RISK GATE. AI prose alone never triggers an order.

## AI numbers not authoritative (§43)
AI does not supply price, P/L, IV, EPS, portfolio value, allowed capital, position size. Application services calculate them.

## AI critic (§44)
Important autonomous decisions challenged independently: contradictory evidence, stale info, provider conflict, weak backtest, fragile parameters, poor sample size, excessive volatility, concentration, catalyst risk, regime mismatch. Valid final result may be NO_TRADE.
```

- [ ] **Step 2: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add docs/V9_AI_SYSTEM.md
git commit -m "docs(v9-phase0): V9_AI_SYSTEM — §34–§44 structure"
```

---

### Task 10: `docs/V9_DATA_POLICY.md`

**Files:**
- Create: `docs/V9_DATA_POLICY.md`

- [ ] **Step 1: Write `docs/V9_DATA_POLICY.md`**

```markdown
# V9_DATA_POLICY

Living document. Structure from spec §27–§33. Implementation `NOT STARTED` at Phase 0; built in Phase 1.

## Canonical data layer (§27)
Entities: symbol, asset, exchange, currency, timezone, trading session, OHLCV, corporate actions, earnings, analyst estimates, fundamentals, options, news, SEC, signals. Prefer UTC internally; exchange-local time only when required.

## Market calendar (§28)
Understand exchange timezone, holidays, half days, pre-market, regular session, after-hours, BMO, AMC. Do not assume every weekday is a trading day.

## Point-in-time data (§29)
Preserve where appropriate: event_time, publication_time, provider_time, received_at, available_from, fetched_at, version. At historical timestamp T, the system may use only data available at T.

## Survivorship bias (§30)
Historical universe tests must not silently use only current surviving securities. Account for delisted stocks, historical index membership, ticker changes, mergers, bankruptcies where data permits. If unavailable, document the limitation; do not pretend it is solved.

## Corporate actions (§31)
Handle splits, dividends, ticker changes, mergers. Define adjusted/raw price conventions clearly.

## No synthetic historical facts (§32)
Do NOT reconstruct unavailable historical facts from future data. Examples: old option IV from today's chain; historical analyst consensus from today's revised value; old sentiment from later articles. Missing historical info remains UNAVAILABLE unless a valid point-in-time source exists.

## Data quality (§33)
Every important analysis exposes: source, timestamp, freshness, coverage, stale, conflicts, quality_flags. Separate SIGNAL/ANALYSIS QUALITY from DATA QUALITY.
```

- [ ] **Step 2: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add docs/V9_DATA_POLICY.md
git commit -m "docs(v9-phase0): V9_DATA_POLICY — §27–§33"
```

---

### Task 11: `docs/V9_RISK_CONSTITUTION.md`

**Files:**
- Create: `docs/V9_RISK_CONSTITUTION.md`

- [ ] **Step 1: Write `docs/V9_RISK_CONSTITUTION.md`**

```markdown
# V9_RISK_CONSTITUTION  — PROTECTED

> **PROTECTED (spec §48, §82).** No autonomous agent may modify this document, the kill switch, promotion gates, critical tests, live-autonomy permissions, holdout policy, or final-holdout policy without explicit owner authorization. Owner action required for any change.

Living document. Fields from spec §47. Values marked `OWNER_MUST_SET` are introduced by V9 and require the owner to set them (Phase 9). Values marked `V7_CURRENT` reflect the existing V7 limit where one exists (to be audited and confirmed in Phase 9).

| Field | Value | Source |
|---|---|---|
| Max risk per trade | OWNER_MUST_SET | §47 |
| Max position size | OWNER_MUST_SET (V7 has max-positions cap, recently raised 10→100) | §47 |
| Max total exposure | OWNER_MUST_SET | §47 |
| Leverage | OWNER_MUST_SET (V7 has 5x path — audit flagged no liquidation guard) | §47 |
| Minimum cash | OWNER_MUST_SET | §47 |
| Max sector exposure | OWNER_MUST_SET | §47 |
| Correlation constraints | OWNER_MUST_SET | §47 |
| Daily loss | OWNER_MUST_SET | §47 |
| Drawdown | OWNER_MUST_SET | §47 |
| Stale-data limits | OWNER_MUST_SET (V7 has data_guard thresholds, audit flagged fail-open) | §47 |
| Data-conflict rules | OWNER_MUST_SET | §47 |
| Strategy evidence requirement | OWNER_MUST_SET (no strategy without validated evidence, §61) | §47 |
| Kill-switch behavior | block new automated risk immediately; monitoring stays active (§74) | §47 |
| Live-autonomy permissions | LIVE_AUTO disabled by default; owner-enabled only (§45, §46) | §47 |

## Goal-hacking protection (§83)
Agents may NOT improve their score by: deleting losing tests; removing transaction costs; changing evaluation period after seeing results; excluding losing securities; reducing sample-size requirements; weakening risk limits; editing holdout; changing target metric after results; lowering promotion threshold.

## Protected files (§82)
Autonomous agents may NOT modify without explicit owner authorization: V9_RISK_CONSTITUTION, critical acceptance policy, constitutional test definitions, production secrets, live autonomy permissions, promotion policy, final holdout policy.

## AI cannot change the Risk Constitution (§48)
No autonomous agent may weaken any of the above. Owner action required.
```

- [ ] **Step 2: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add docs/V9_RISK_CONSTITUTION.md
git commit -m "docs(v9-phase0): V9_RISK_CONSTITUTION — PROTECTED, §47/§48/§82/§83"
```

---

### Task 12: `docs/V9_RESEARCH_FACTORY.md`

**Files:**
- Create: `docs/V9_RESEARCH_FACTORY.md`

- [ ] **Step 1: Write `docs/V9_RESEARCH_FACTORY.md`**

```markdown
# V9_RESEARCH_FACTORY

Living document. Structure from spec §3, §75–§79. Implementation `NOT STARTED` at Phase 0; built in Phases 14–16. Separate from production execution.

## Self-improvement loop (§3)
OBSERVE → MEASURE → IDENTIFY WEAKNESS → FORM TESTABLE HYPOTHESIS → CREATE CONTROLLED EXPERIMENT → CREATE CHALLENGER → BACKTEST → OUT-OF-SAMPLE VALIDATION → ROBUSTNESS TESTING → SHADOW / PAPER TESTING → COMPARE WITH CHAMPION → PROMOTE OR REJECT → MONITOR → ROLL BACK IF REQUIRED.

Self-improvement MUST NOT mean: RECENT LOSS → AI RANDOMLY CHANGES PARAMETERS → DEPLOY TO PRODUCTION. More runtime generates more evidence, not more risk.

## Research agents (§75)
Research Director, Hypothesis Agent, Experiment Planner, Quant Validator, Coding Agent, Code Review Agent, Security Review Agent, Test Agent, Release Agent.

## Hypothesis output (§76)
Each hypothesis structured: hypothesis_id, problem, observation, hypothesis, expected mechanism, baseline, challenger, universe, horizon, dataset, metrics, promotion criteria, failure criteria, risks. No vague "maybe RSI should be changed."

## Experiment registry statuses (§77)
PROPOSED, APPROVED_FOR_RESEARCH, RUNNING, FAILED, REJECTED, VALIDATED, SHADOW, PROMOTED, ROLLED_BACK.

## Research memory (§78)
Structured relationships between experiments, strategies, features, ML models, AI models, prompts, decisions, positions, market regimes, failures, improvements. Numeric authoritative facts stay in structured data (vector memory is not authoritative portfolio state).

## Concept drift (§79)
Monitor feature distributions, probability calibration, win rate, expectancy, drawdown, strategy behavior, sentiment distributions, AI decision distributions. Drift triggers REVALIDATION, not AUTOMATIC RISK INCREASE.
```

- [ ] **Step 2: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add docs/V9_RESEARCH_FACTORY.md
git commit -m "docs(v9-phase0): V9_RESEARCH_FACTORY — §3, §75–§79"
```

---

### Task 13: `docs/V9_OPERATIONS.md`

**Files:**
- Create: `docs/V9_OPERATIONS.md`

- [ ] **Step 1: Write `docs/V9_OPERATIONS.md`**

```markdown
# V9_OPERATIONS

Living document. Structure from spec §45, §74, §98, §103, §104–§106. Implementation `NOT STARTED` at Phase 0; built across phases.

## Autonomy modes (§45)
OFF, ANALYZE, RECOMMEND, PAPER_AUTO, LIVE_ASSISTED, LIVE_AUTO. Default = OFF (NOT LIVE_AUTO). LIVE_AUTO may only be enabled explicitly by the owner.

## Paper auto before live auto (§46)
The full V9 decision loop must first prove itself in PAPER_AUTO. LIVE_AUTO stays disabled until mandatory Paper-Auto acceptance tests pass.

## Kill switch (§74)
Prominent "STOP AUTONOMOUS TRADING" control. Immediately blocks new automated risk. Monitoring remains active.

## System health (§98)
Real health checks for: backend, DB, scheduler, market provider, earnings, options, news, SEC, AI providers, ML model, Paper Broker, live broker if configured, research worker, MLflow if used, RD-Agent worker if used. No fake green status.

## Job states (§103)
QUEUED, RUNNING, PROGRESS, COMPLETE, FAILED, CANCELLED. Heavy computation (scans, backtests, AI batches, ML training, Monte Carlo, research) must not freeze the app (§102).

## Migrations (§104)
Use DB migrations. Test: REALISTIC V7 SCHEMA COPY → V9 MIGRATION → V9 START → V7 DATA PRESERVED. Do not delete the database for convenience.

## Backups and rollback (§105)
Before destructive migration: create verified backup in dev/test workflow; provide migration rollback strategy when practical.

## Secrets (§106)
Never commit API keys; never store secrets in prompts; never expose keys to frontend; never expose broker credentials to Coding AI; never log tokens.

## Crash recovery (V7 audit F5, to be addressed)
V7 has no crash recovery on Windows (engine poll() reports but does not respawn). V9 operations must add a watchdog. To be designed in Phase 18.
```

- [ ] **Step 2: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add docs/V9_OPERATIONS.md
git commit -m "docs(v9-phase0): V9_OPERATIONS — §45/§74/§98/§103–§106"
```

---

### Task 14: `docs/V9_CHANGELOG.md`

**Files:**
- Create: `docs/V9_CHANGELOG.md`

- [ ] **Step 1: Write `docs/V9_CHANGELOG.md`**

```markdown
# V9_CHANGELOG

## 2026-08-11 — Phase 0
- Migrated the read-only V7 codebase (`Market Signals V7 - Premium` @ f912a33) into the V9 directory as a clean copy (code + preserved data + V7 system docs); excluded audit-flagged junk (504 MB logs, 425 MB tmp, __pycache__, V7 .git), secrets (config.json), and debug artifacts.
- Scrubbed any hardcoded API keys to REDACTED; seeded V9 `config.json` from `config.example.json` (placeholder keys, gitignored).
- Established the V9 baseline by execution: app import OK; regression suite and smoke gate run with results recorded in `docs/v9-baseline-results.md`.
- Created the 10 V9 control documents as living skeletons: V9_IMPLEMENTATION, V9_ARCHITECTURE, V9_TEST_MATRIX, V9_OPEN_SOURCE_MANIFEST, V9_AI_SYSTEM, V9_DATA_POLICY, V9_RISK_CONSTITUTION (PROTECTED), V9_RESEARCH_FACTORY, V9_OPERATIONS, V9_CHANGELOG.
- No V7 code or data was modified; V7 git state unchanged (f912a33, 109 dirty).
- Phase 0 status: PASS (per Task 15 gate). Next: Phase 1 — Data & Architecture Foundation.
```

- [ ] **Step 2: Commit**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add docs/V9_CHANGELOG.md
git commit -m "docs(v9-phase0): V9_CHANGELOG — Phase 0 entry"
```

---

### Task 15: Phase 0 exit gate verification + final checkpoint

**Files:**
- Modify: `docs/V9_IMPLEMENTATION.md` (set Phase 0 status `IN PROGRESS` → `PASS`, set gate result)
- Create: none (verification only)

**Interfaces:**
- Consumes: all prior tasks' outputs.
- Produces: Phase 0 = `PASS` (or NOT DONE with exact failing items), final checkpoint commit.

- [ ] **Step 1: Verify V7 untouched (gate criterion 1)**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V7 - Premium"
echo "head=$(git rev-parse --short HEAD) dirty=$(git status --short | wc -l)"
```
Expected: `head=f912a33 dirty=109` == `docs/v7-source-baseline.txt`. If not, STOP — Phase 0 = NOT DONE.

- [ ] **Step 2: Verify migration clean (gate criteria 2, 3)**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
echo "code dirs:"; ls -d api core agents tools frontend tests tests_e2e 2>&1
echo "no logs:"; ls -d logs 2>&1 || echo "absent"
echo "no tmp:"; find . -name '*.tmp' 2>/dev/null | head -1 || echo "none"
echo "no pycache:"; find . -type d -name __pycache__ 2>/dev/null | head -1 || echo "none"
echo "no hardcoded 32-hex keys in code:"; grep -rEnE '[0-9a-f]{32}' --include=*.py api core agents tools tests 2>/dev/null | head -5 || echo "none"
```
Expected: code dirs present; logs absent; no .tmp; no __pycache__; no genuine 32-hex API keys (false-positive hash constants are acceptable and noted).

- [ ] **Step 3: Verify baseline green (gate criterion 7)**

Open `docs/v9-baseline-results.md` and confirm: regression suite exit 0, delta vs V7 §9 (557 passed) = 0 new failures. If new failures exist and were not resolved, Phase 0 = NOT DONE.

- [ ] **Step 4: Verify all 10 control docs exist with mandated structure (gate criteria 4, 5, 6)**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
for f in V9_IMPLEMENTATION V9_ARCHITECTURE V9_TEST_MATRIX V9_OPEN_SOURCE_MANIFEST V9_AI_SYSTEM V9_DATA_POLICY V9_RISK_CONSTITUTION V9_RESEARCH_FACTORY V9_OPERATIONS V9_CHANGELOG; do
  test -f "docs/$f.md" && echo "OK docs/$f.md" || echo "MISSING docs/$f.md"
done
echo "--- IMPLEMENTATION has 20 phases ---"; grep -c "^| [0-9]" docs/V9_IMPLEMENTATION.md
echo "--- IMPLEMENTATION Phase 0 PASS ---"; grep -c "Phase 0.*PASS\|0 |.*PASS" docs/V9_IMPLEMENTATION.md
echo "--- MANIFEST has 12+ repos ---"; grep -cE "github.com" docs/V9_OPEN_SOURCE_MANIFEST.md
echo "--- RISK_CONSTITUTION PROTECTED ---"; grep -c "PROTECTED" docs/V9_RISK_CONSTITUTION.md
```
Expected: all 10 `OK`; IMPLEMENTATION has 20 phase rows; Phase 0 marked PASS; MANIFEST ≥ 12 github.com rows; RISK_CONSTITUTION contains PROTECTED.

- [ ] **Step 5: Set Phase 0 = PASS in `docs/V9_IMPLEMENTATION.md`**

Using `Edit`, change the Phase 0 table row status from `IN PROGRESS` to `PASS`:
- old: `| 0 | V7 Full Audit (existing V7 audit reused) + V7→V9 migration + baseline + V9 control docs | IN PROGRESS |`
- new: `| 0 | V7 Full Audit (existing V7 audit reused) + V7→V9 migration + baseline + V9 control docs | PASS |`

And in the Phase 0 record:
- old: `- **Status:** IN PROGRESS → PASS (set to PASS by Task 15 gate).`
- new: `- **Status:** PASS — all 15-task gate criteria verified (V7 untouched, migration clean, baseline green, 10 docs created, manifest 12 repos PENDING).`

If any gate criterion failed, instead set status `FAILED` and list the exact failing items (spec §122).

- [ ] **Step 6: Final checkpoint commit (no remote push, spec §116)**

```bash
cd "C:\Users\<user>\PyCharmMiscProject\Market Signals V9"
git add docs/V9_IMPLEMENTATION.md
git commit -m "chore(v9-phase0): Phase 0 = PASS — exit gate verified; local checkpoint"
git log --oneline | head -20
```

- [ ] **Step 7: Report Phase 0 result**

State the result plainly (spec §4: no forbidden success language):
- If all gates passed: "Phase 0 = PASS. V9 contains the migrated codebase + baseline + 10 control documents. V7 untouched. Next phase: Phase 1 — Data & Architecture Foundation."
- If any gate failed: "Phase 0 = NOT DONE. Failing items: <exact list>." Do not claim completion.

---

## Self-Review

**Spec coverage:** spec §5 (no rewrite) → Tasks 2 (verbatim copy) + 6 (zero code change). §6 (baseline) → Task 4 (executed runs). §7 (preserve) → Task 2 INCLUDE_DATA + Task 5 reused-components list. §8 (10 docs) → Tasks 5–14. §9 (IMPLEMENTATION structure) → Task 5. §10 (manifest) → Task 8. §116 (checkpoint, no push) → every task's commit + Task 15. §117 (don't touch V7) → gate criterion 1 + every task's Step "V7 untouched". §106 (no secrets) → Task 3 scrub + gitignore. §115 (no test weakening) → Task 4 Step 2. Covered.

**Placeholder scan:** doc skeletons use §9 status vocabulary and "to be populated in Phase N" — intentional living-doc mechanism, not forbidden placeholders. No "TBD" except in the manifest's per-repo `commit/release`/`license` cells, which are explicitly `PENDING` for Phase 2 (spec-mandated). Acceptable.

**Type consistency:** `docs/v7-source-baseline.txt` written in Task 1, read in Task 2 Step 4, Task 15 Step 1 — same fields `v7_head`/`v7_dirty`. `docs/v9-baseline-results.md` produced in Task 4, consumed in Task 5 and verified in Task 15 Step 3 — consistent. Gate criteria referenced consistently across Tasks 1, 2, 3, 15. No naming drift.