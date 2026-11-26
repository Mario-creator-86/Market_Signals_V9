"""
Capture exact test failure messages for the 2 suite-only failures.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
OUT = ROOT / "tests" / "failure_details2.txt"

result = subprocess.run(
    [sys.executable, "-m", "pytest",
     "tests/unit",
     "tests/integration",
     "-v", "--tb=long", "-q"],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
)

combined = result.stdout + "\n\n--- STDERR ---\n" + result.stderr
OUT.write_text(combined, encoding="utf-8")
print(combined[-4000:])
print("Exit code:", result.returncode)
