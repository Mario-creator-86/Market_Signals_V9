"""
Baseline test runner for Market Signals V7 - Premium audit.
Runs all automated tests and saves results to a log file.
Usage: python scripts/baseline_test_run.py
"""
import subprocess
import sys
import os
import json
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
REPORT_PATH = ROOT / "scripts" / "baseline_results.json"

def run_suite(name, args):
    print(f"\n{'='*60}")
    print(f"Running: {name}")
    print(f"Command: {' '.join(args)}")
    print('='*60)
    start = time.time()
    result = subprocess.run(
        args,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    elapsed = time.time() - start
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    print(stdout[-3000:] if len(stdout) > 3000 else stdout)
    if stderr:
        print("STDERR:", stderr[-1000:] if len(stderr) > 1000 else stderr)
    return {
        "name": name,
        "returncode": result.returncode,
        "elapsed_sec": round(elapsed, 2),
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-500:],
        "passed": result.returncode == 0,
    }

def main():
    results = []

    # Unit tests
    results.append(run_suite(
        "unit",
        [sys.executable, "-m", "pytest", "tests/unit", "-q", "--tb=short", "--timeout=30", "-x"]
    ))

    # Integration tests
    results.append(run_suite(
        "integration",
        [sys.executable, "-m", "pytest", "tests/integration", "-q", "--tb=short", "--timeout=60", "-x"]
    ))

    # Contract tests
    results.append(run_suite(
        "contract",
        [sys.executable, "-m", "pytest", "tests/contract", "-q", "--tb=short", "--timeout=30"]
    ))

    # Perf (smoke only)
    results.append(run_suite(
        "perf",
        [sys.executable, "-m", "pytest", "tests/perf", "-q", "--tb=short", "--timeout=30"]
    ))

    # Root-level tests (non-ignored)
    results.append(run_suite(
        "root_tests",
        [sys.executable, "-m", "pytest", "tests", "-q", "--tb=no", "--timeout=30",
         "--ignore=tests/unit", "--ignore=tests/integration", "--ignore=tests/contract",
         "--ignore=tests/perf", "--ignore=tests/e2e", "--ignore=tests/golden"]
    ))

    # Summary
    print("\n\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"  [{status}] {r['name']:20s} ({r['elapsed_sec']}s)")

    total_pass = sum(1 for r in results if r["passed"])
    total = len(results)
    print(f"\nTotal: {total_pass}/{total} suites passed")

    with open(REPORT_PATH, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved results to {REPORT_PATH}")

if __name__ == "__main__":
    main()
