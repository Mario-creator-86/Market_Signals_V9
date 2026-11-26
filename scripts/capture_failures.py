"""
Capture exact test failure messages.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
OUT = ROOT / "tests" / "failure_details.txt"

result = subprocess.run(
    [sys.executable, "-m", "pytest",
     "tests/integration/test_state_dashboard_versioning.py::test_dashboard_endpoint_supports_if_version_unchanged",
     "tests/unit/test_news_tab_on_demand_cache.py::test_ensure_ticker_news_cached_fetches_once_then_hits",
     "tests/unit/test_news_tab_on_demand_cache.py::test_ensure_ticker_news_cached_throttles_empty_miss",
     "-v", "--tb=long"],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
)

combined = result.stdout + "\n\n--- STDERR ---\n" + result.stderr
OUT.write_text(combined, encoding="utf-8")
print(combined)
