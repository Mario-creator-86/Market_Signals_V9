PYTHON ?= python
PYTEST ?= pytest
RUN_E2E ?= 0

.PHONY: test-backend test-frontend test-contract test-smoke test-all supply-chain release-check

test-backend:
	$(PYTHON) tools/test_runner.py backend --run-e2e $(RUN_E2E)

test-frontend:
	$(PYTHON) tools/test_runner.py frontend --run-e2e $(RUN_E2E)

test-contract:
	$(PYTHON) tools/test_runner.py contract --run-e2e $(RUN_E2E)

test-smoke:
	$(PYTHON) tools/test_runner.py smoke --run-e2e $(RUN_E2E)

test-all:
	$(PYTHON) tools/test_runner.py all --run-e2e $(RUN_E2E)

supply-chain:
	$(PYTHON) tools/supply_chain_check.py

release-check:
	$(PYTHON) tools/release_check.py
