"""
Pytest root configuration.

Collection is restricted via `testpaths` in pytest.ini (tests/ and tests_e2e/),
so loose scripts at the repo root are never collected. e2e suites are
additionally gated behind RUN_E2E=1 in their own conftests.
"""
