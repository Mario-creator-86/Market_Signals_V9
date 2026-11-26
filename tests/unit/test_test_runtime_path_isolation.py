from pathlib import Path


def test_pytest_mutable_runtime_paths_are_disposable_and_coherent():
    import core.config as config
    import core.capital_manager as capital
    import core.sqlite_store as sqlite_store

    runtime = Path(config.RUNTIME_DIR).resolve()
    data = Path(config.DATA_DIR).resolve()
    project = Path(config.BASE_DIR).resolve()

    assert runtime != (project / "runtime").resolve()
    assert data != (project / "data").resolve()
    assert Path(config.CONFIG_PATH).resolve().is_relative_to(runtime)
    assert Path(config.STATE_PATH).resolve().is_relative_to(runtime)
    assert Path(config.SIGNALS_PATH).resolve().is_relative_to(runtime)
    assert Path(capital.CAPITAL_STATE_PATH).resolve().is_relative_to(runtime)
    assert Path(sqlite_store.DB_PATH).resolve().is_relative_to(data)
