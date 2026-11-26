from tools import test_runner


def test_backend_runner_includes_root_and_acceptance_tests():
    assert "tests" in test_runner.BACKEND_SUITES
    assert "--ignore=tests/e2e" in test_runner.BACKEND_SUITES


def test_frontend_runner_uses_complete_canonical_e2e_suite():
    assert test_runner.E2E_SUITES == ("tests_e2e",)


def test_e2e_runner_allocates_an_isolated_port(monkeypatch):
    monkeypatch.delenv("E2E_BASE_URL", raising=False)
    monkeypatch.delenv("E2E_PORT", raising=False)

    environment = test_runner._base_env(run_e2e=True)

    assert int(environment["E2E_PORT"]) > 0
