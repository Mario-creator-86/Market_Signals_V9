"""Wave 3 — data isolation and temp-file hygiene.

Two defects found on 2026-11-15 when a second app instance was started with
``MS_DATA_DIR`` / ``MS_RUNTIME_DIR`` / ``MS_CONFIG_PATH`` pointing at empty
folders:

1. It still opened the owner's real ``data/market_signals.db`` and served the
   real watchlist and capital state, because many modules hard-coded
   ``<repo>/data/...`` (or a path relative to the working directory) instead
   of the configured data folder.
2. ``data/`` and ``runtime/`` held 149 orphaned temp files (2.2 GB) left behind
   by atomic writes that were interrupted; nothing ever deleted them.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import core.config as config
import core.sqlite_store as sqlite_store
import core.state_persistence as state_persistence
import core.state_store as state_store

REPO = Path(__file__).resolve().parents[2]


# ───────────────────────── Task 1: one source of truth ─────────────────────

_PROBE = r'''
import importlib, json, os, pkgutil, sys, types
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
sys.path.insert(0, str(repo))
try:  # the project .env must not re-introduce live paths or keys
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: None
except ImportError:
    pass

out = {"failed": {}, "named": {}, "leaks": {}, "errors": {}}


def record(label, getter):
    try:
        value = getter()
        out["named"][label] = str(Path(value).resolve())
    except BaseException as exc:  # reported, never swallowed
        out["errors"][label] = f"{type(exc).__name__}: {exc}"[:300]


def attr(module_name, name):
    return lambda: getattr(importlib.import_module(module_name), name)


LIGHT = [
    ("core.config", "DATA_DIR"), ("core.config", "RUNTIME_DIR"),
    ("core.config", "CONFIG_PATH"), ("core.config", "STATE_PATH"),
    ("core.config", "SIGNALS_PATH"), ("core.config", "WATCHLIST_PATH"),
    ("core.config", "SYMBOL_CACHE_PATH"), ("core.config", "PRESETS_PATH"),
    ("core.config", "HOTLIST_CACHE_PATH"), ("core.config", "SUPPRESSED_TICKERS_PATH"),
    ("core.config", "BACKTEST_RESULTS_PATH"), ("core.config", "TRADE_RADAR_STATE_PATH"),
    ("core.sqlite_store", "DB_PATH"),
    ("core.data_model.store", "DB_PATH"),
    ("core.data_model.survivorship", "DB_PATH"),
    ("core.autonomous_control", "REQUEST_PATH"),
    ("core.autonomous_control", "STATUS_PATH"),
    ("core.autonomous_control", "ALLOCATION_STATE_PATH"),
    ("core.observability", "_ENGINE_STATUS_DUMP_PATH"),
    ("api.agent_runtime_service", "_ENGINE_HEARTBEAT_PATH"),
    ("core.trade_audit", "TRADE_AUDIT_PATH"),
    ("core.rdagent_worker", "DEFAULT_VENV_PATH"),
    ("core.rdagent_worker", "DEFAULT_WORKSPACE"),
    ("core.rdagent_worker", "STATUS_PATH"),
]
HEAVY = [
    ("api.cockpit_service", "_DB_PATH"),
    ("core.ai.base_rates", "DB_PATH"),
    ("core.development_factory.watchdog", "DEFAULT_CASES_PATH"),
    ("core.development_factory.watchdog", "DEFAULT_ACTIVITY_PATH"),
    ("core.execution_archive", "ARCHIVE_DIR"),
    ("core.execution_archive", "STOCKS_FILE"),
    ("core.execution_archive", "CRYPTO_FILE"),
    ("core.sec.service", "SEC_CACHE_PATH"),
    ("core.entry_chart_snapshot", "ENTRY_CHART_DIR"),
    ("core.capital_manager", "CAPITAL_STATE_PATH"),
    ("core.analysis_store", "DATA_FILE"),
    ("core.feedback_loop", "FEEDBACK_JOURNAL_PATH"),
    ("api.hotlist_service", "DEEP_STORE_PATH"),
]

for module_name, name in LIGHT:
    record(f"{module_name}.{name}", attr(module_name, name))

import core.config as cfg
import core.sqlite_store as sq

record("config.data_path(yfinance_cache)", lambda: cfg.data_path("yfinance_cache"))
record("config.data_path(cache_backtest)", lambda: cfg.data_path("cache_backtest"))
record("config.runtime_path(x.json)", lambda: cfg.runtime_path("x.json"))
record("config.resolve_state_path(data/execution.db)", lambda: cfg.resolve_state_path("data/execution.db"))
record("config.resolve_state_path(runtime/x.json)", lambda: cfg.resolve_state_path("runtime/x.json"))
record("config.DEFAULT_CONFIG[sec][cache_path]", lambda: cfg.DEFAULT_CONFIG["sec"]["cache_path"])
out["sqlite_source"] = sq.DB_PATH_SOURCE

if mode == "isolated":
    # Every module of the app, so a path constant added anywhere is seen.
    names = []
    for package in ("core", "api", "agents"):
        root = importlib.import_module(package)
        for info in pkgutil.walk_packages(root.__path__, prefix=package + "."):
            names.append(info.name)
    for name in names:
        try:
            importlib.import_module(name)
        except BaseException as exc:
            out["failed"][name] = f"{type(exc).__name__}: {exc}"[:200]
    out["module_count"] = len(names)

    for module_name, name in HEAVY:
        record(f"{module_name}.{name}", attr(module_name, name))

    from core.execution.runtime import execution_store_path
    from core.decision_ledger.store import decision_ledger_store_path
    from core.development_factory import watchdog
    from core.backtest_engine import BacktestEngine
    from core.hotlist_module import HotlistModule

    record("execution_store_path()", lambda: execution_store_path(None))
    record(
        "execution_store_path(config default)",
        lambda: execution_store_path({"paper_execution": dict(cfg.DEFAULT_CONFIG["paper_execution"])}),
    )
    record("decision_ledger_store_path()", lambda: decision_ledger_store_path(None))
    record(
        "decision_ledger_store_path(data/x.db)",
        lambda: decision_ledger_store_path({"decision_ledger": {"store_path": "data/x.db"}}),
    )
    record("watchdog.default_cases_path()", watchdog.default_cases_path)
    record("watchdog.default_activity_path()", watchdog.default_activity_path)
    record("BacktestEngine().cache_dir", lambda: BacktestEngine().cache_dir)
    record("HotlistModule().cache_dir", lambda: HotlistModule().cache_dir)

    # A canary proves the scan below can see a leak at all.
    canary = types.ModuleType("core._wave3_isolation_canary")
    canary.LEAK = repo / "data" / "canary.json"
    sys.modules[canary.__name__] = canary

    forbidden = [repo / "data", repo / "runtime"]
    for name, module in sorted(sys.modules.items()):
        if module is None or not name.startswith(("core", "api", "agents")):
            continue
        for key, value in list(vars(module).items()):
            if isinstance(value, Path):
                candidate = value
            elif (
                isinstance(value, str) and 0 < len(value) < 400
                and "\n" not in value and (os.sep in value or "/" in value)
            ):
                candidate = Path(value)
            else:
                continue
            try:
                resolved = candidate.resolve()
            except Exception:
                continue
            if any(resolved == root or root in resolved.parents for root in forbidden):
                out["leaks"][f"{name}.{key}"] = str(resolved)

print("@@PROBE@@" + json.dumps(out))
'''


def _run_probe(tmp_path: Path, mode: str) -> dict:
    script = tmp_path / "probe.py"
    script.write_text(_PROBE, encoding="utf-8")
    work = tmp_path / "elsewhere"  # the server may be started from any folder
    work.mkdir()
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("MS_") and key != "TEST_MODE"
    }
    env.update({
        "PYTHONUTF8": "1",
        "TEST_MODE": "1",  # no legacy-layout migration, no autostart
        "MS_DISABLE_ENGINE_AUTOSTART": "1",
        "MS_DISABLE_WARMUP": "1",
        "MS_DISABLE_STATE_PREFETCH": "1",
        "MS_DISABLE_BG_WORKER": "1",
        "MS_ENGINE_MODE": "disabled",
        # Logs/audit/backups are separate folders with their own variables;
        # they are redirected so the probe writes nothing into the repo.
        "MS_LOG_DIR": str(tmp_path / "logs"),
        "MS_AUDIT_LOG_DIR": str(tmp_path / "audit_logs"),
        "MS_BACKUP_DIR": str(tmp_path / "backups"),
    })
    if mode == "isolated":
        env.update({
            "MS_ENV": "test",
            "MS_DATA_DIR": str(tmp_path / "data"),
            "MS_RUNTIME_DIR": str(tmp_path / "runtime"),
            "MS_CONFIG_PATH": str(tmp_path / "runtime" / "config.json"),
        })
    completed = subprocess.run(
        [sys.executable, str(script), str(REPO), mode],
        cwd=str(work), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=420,
    )
    marker = [line for line in completed.stdout.splitlines() if line.startswith("@@PROBE@@")]
    assert marker, (
        f"probe produced no result (exit {completed.returncode}):\n"
        f"{completed.stdout[-1500:]}\n{completed.stderr[-3000:]}"
    )
    return json.loads(marker[-1][len("@@PROBE@@"):])


@pytest.mark.timeout(480)
def test_instance_with_its_own_folders_resolves_every_path_inside_them(tmp_path):
    """MS_DATA_DIR + MS_RUNTIME_DIR + MS_CONFIG_PATH only, none of the specific
    overrides, started from another working directory: every state path lives
    in the instance's own folders and nothing resolves under the repo's
    ``data/`` or ``runtime/``."""
    result = _run_probe(tmp_path, "isolated")

    assert result["failed"] == {}, f"modules failed to import: {result['failed']}"
    assert result["errors"] == {}, f"paths could not be resolved: {result['errors']}"
    assert result["module_count"] > 200

    leaks = dict(result["leaks"])
    canary = leaks.pop("core._wave3_isolation_canary.LEAK", None)
    assert canary, "the leak scan did not see its own canary; it proves nothing"
    assert leaks == {}, (
        "module-level paths still point into the repo's data/ or runtime/ "
        f"although MS_DATA_DIR / MS_RUNTIME_DIR are set: {leaks}"
    )

    own = [(tmp_path / "data").resolve(), (tmp_path / "runtime").resolve()]
    escaped = {
        label: value for label, value in result["named"].items()
        if not any(Path(value) == root or root in Path(value).parents for root in own)
    }
    assert escaped == {}, f"paths outside the instance's own folders: {escaped}"
    assert len(result["named"]) >= 50

    named = result["named"]
    data, runtime = own
    # The database is the instance's own, and says why.
    assert Path(named["core.sqlite_store.DB_PATH"]) == data / "market_signals.db"
    assert result["sqlite_source"] == "MS_DATA_DIR"
    for label in (
        "core.data_model.store.DB_PATH", "core.data_model.survivorship.DB_PATH",
        "api.cockpit_service._DB_PATH", "core.ai.base_rates.DB_PATH",
    ):
        assert named[label] == named["core.sqlite_store.DB_PATH"], label
    # Engine heartbeat: the writer (engine) and the reader (API) agree.
    assert (
        named["core.observability._ENGINE_STATUS_DUMP_PATH"]
        == named["api.agent_runtime_service._ENGINE_HEARTBEAT_PATH"]
        == str(data / "agent_status_engine.json")
    )
    # Autonomous control files: no cross-talk between two instances' engines.
    assert Path(named["core.autonomous_control.REQUEST_PATH"]) == data / "autonomous_cycle_request.json"
    assert Path(named["core.autonomous_control.STATUS_PATH"]) == data / "autonomous_cycle_status.json"
    assert Path(named["execution_store_path()"]) == data / "execution.db"
    assert Path(named["execution_store_path(config default)"]) == data / "execution.db"
    assert Path(named["decision_ledger_store_path()"]) == data / "decision_ledger.db"
    assert Path(named["core.rdagent_worker.DEFAULT_WORKSPACE"]) == runtime / "rdagent_workspace"


def test_default_layout_without_env_vars_is_unchanged(tmp_path):
    """No MS_* path variable: exactly the locations used before, and they do
    not depend on the working directory the process was started from."""
    result = _run_probe(tmp_path, "default")
    assert result["errors"] == {}, result["errors"]

    data, runtime = (REPO / "data").resolve(), (REPO / "runtime").resolve()
    expected = {
        "core.config.DATA_DIR": data,
        "core.config.RUNTIME_DIR": runtime,
        "core.config.CONFIG_PATH": runtime / "config.json",
        "core.config.STATE_PATH": runtime / "state.json",
        "core.config.SIGNALS_PATH": runtime / "signals_state.json",
        "core.config.WATCHLIST_PATH": runtime / "watchlist.json",
        "core.config.SYMBOL_CACHE_PATH": runtime / "symbol_status.json",
        "core.config.PRESETS_PATH": runtime / "presets.json",
        "core.config.HOTLIST_CACHE_PATH": runtime / "hotlist_cache.json",
        "core.config.SUPPRESSED_TICKERS_PATH": runtime / "suppressed_tickers.json",
        "core.config.BACKTEST_RESULTS_PATH": data / "backtest_results.json",
        "core.config.TRADE_RADAR_STATE_PATH": data / "trade_radar_state.json",
        "core.sqlite_store.DB_PATH": data / "market_signals.db",
        "core.data_model.store.DB_PATH": data / "market_signals.db",
        "core.data_model.survivorship.DB_PATH": data / "market_signals.db",
        "core.autonomous_control.REQUEST_PATH": data / "autonomous_cycle_request.json",
        "core.autonomous_control.STATUS_PATH": data / "autonomous_cycle_status.json",
        "core.autonomous_control.ALLOCATION_STATE_PATH": data / "autonomous_allocation_state.json",
        "core.observability._ENGINE_STATUS_DUMP_PATH": data / "agent_status_engine.json",
        "api.agent_runtime_service._ENGINE_HEARTBEAT_PATH": data / "agent_status_engine.json",
        "core.trade_audit.TRADE_AUDIT_PATH": data / "trade_decisions.json",
        "core.rdagent_worker.DEFAULT_VENV_PATH": runtime / "rdagent_venv",
        "core.rdagent_worker.DEFAULT_WORKSPACE": runtime / "rdagent_workspace",
        "core.rdagent_worker.STATUS_PATH": runtime / "rdagent_status.json",
        "config.data_path(yfinance_cache)": data / "yfinance_cache",
        "config.data_path(cache_backtest)": data / "cache_backtest",
        "config.runtime_path(x.json)": runtime / "x.json",
        "config.resolve_state_path(data/execution.db)": data / "execution.db",
        "config.resolve_state_path(runtime/x.json)": runtime / "x.json",
        "config.DEFAULT_CONFIG[sec][cache_path]": data / "sec_filings_cache.json",
    }
    assert {label: Path(value) for label, value in result["named"].items()} == expected
    assert result["sqlite_source"] == "default"


def test_paths_in_this_test_session_follow_the_configured_folders():
    """Same rule, in process: the constants derive from DATA_DIR / RUNTIME_DIR."""
    from api import agent_runtime_service, cockpit_service
    from core import autonomous_control, execution_archive, observability, rdagent_worker
    from core.data_model import store as pit_store, survivorship
    from core.development_factory import watchdog
    from core.sec import service as sec_service

    data, runtime = Path(config.DATA_DIR), Path(config.RUNTIME_DIR)
    assert autonomous_control.REQUEST_PATH == data / "autonomous_cycle_request.json"
    assert autonomous_control.STATUS_PATH == data / "autonomous_cycle_status.json"
    assert autonomous_control.ALLOCATION_STATE_PATH == data / "autonomous_allocation_state.json"
    assert observability._ENGINE_STATUS_DUMP_PATH == data / "agent_status_engine.json"
    assert agent_runtime_service._ENGINE_HEARTBEAT_PATH == observability._ENGINE_STATUS_DUMP_PATH
    assert execution_archive.ARCHIVE_DIR == data
    assert sec_service.SEC_CACHE_PATH == data / "sec_filings_cache.json"
    assert Path(config.DEFAULT_CONFIG["sec"]["cache_path"]) == data / "sec_filings_cache.json"
    assert watchdog.DEFAULT_CASES_PATH == data / "auto_repair_cases.json"
    assert watchdog.DEFAULT_ACTIVITY_PATH == data / "auto_repair_activity.json"
    assert rdagent_worker.DEFAULT_VENV_PATH == runtime / "rdagent_venv"
    assert rdagent_worker.DEFAULT_WORKSPACE == runtime / "rdagent_workspace"
    assert pit_store.DB_PATH == survivorship.DB_PATH == cockpit_service._DB_PATH == Path(sqlite_store.DB_PATH)


def test_resolve_state_path_never_uses_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data, runtime = Path(config.DATA_DIR), Path(config.RUNTIME_DIR)
    assert config.data_path("a", "b.json") == data / "a" / "b.json"
    assert config.runtime_path("c.json") == runtime / "c.json"
    assert config.resolve_state_path("data/execution.db") == data / "execution.db"
    assert config.resolve_state_path("./data/sub/x.db") == data / "sub" / "x.db"
    assert config.resolve_state_path("runtime/x.json") == runtime / "x.json"
    assert config.resolve_state_path("custom/x.db") == config.BASE_DIR / "custom" / "x.db"
    absolute = tmp_path / "own" / "x.db"
    assert config.resolve_state_path(str(absolute)) == absolute
    assert config.resolve_state_path("", data / "fallback.db") == data / "fallback.db"
    assert config.resolve_state_path(None, data / "fallback.db") == data / "fallback.db"
    with pytest.raises(ValueError):
        config.resolve_state_path("")
    # data_path follows a redirected data folder (tests monkeypatch DATA_DIR).
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "redirected")
    assert config.data_path("x.json") == tmp_path / "redirected" / "x.json"
    assert config.resolve_state_path("data/x.json") == tmp_path / "redirected" / "x.json"


def test_specific_overrides_keep_priority(monkeypatch, tmp_path):
    from core.decision_ledger.store import decision_ledger_store_path
    from core.development_factory import watchdog
    from core.execution.runtime import execution_store_path

    monkeypatch.setenv("MS_EXECUTION_DB_PATH", str(tmp_path / "own-exec.db"))
    assert execution_store_path({"paper_execution": {"store_path": "data/execution.db"}}) == tmp_path / "own-exec.db"
    monkeypatch.delenv("MS_EXECUTION_DB_PATH")
    assert execution_store_path(None) == Path(config.DATA_DIR) / "execution.db"
    assert execution_store_path({"paper_execution": {"store_path": "data/execution.db"}}) == (
        Path(config.DATA_DIR) / "execution.db"
    )
    assert execution_store_path({"paper_execution": {"store_path": str(tmp_path / "cfg.db")}}) == tmp_path / "cfg.db"

    monkeypatch.setenv("MS_DECISION_LEDGER_PATH", str(tmp_path / "own-ledger.db"))
    assert decision_ledger_store_path({"decision_ledger": {"store_path": "data/x.db"}}) == tmp_path / "own-ledger.db"
    monkeypatch.delenv("MS_DECISION_LEDGER_PATH")
    assert decision_ledger_store_path(None) == Path(config.DATA_DIR) / "decision_ledger.db"
    assert decision_ledger_store_path({"decision_ledger": {"store_path": "data/x.db"}}) == (
        Path(config.DATA_DIR) / "x.db"
    )

    monkeypatch.setenv("MS_AUTO_REPAIR_CASES_PATH", str(tmp_path / "cases.json"))
    assert watchdog.default_cases_path() == tmp_path / "cases.json"
    monkeypatch.delenv("MS_AUTO_REPAIR_CASES_PATH")
    assert watchdog.default_cases_path() == Path(config.DATA_DIR) / "auto_repair_cases.json"


# Hard-coded "data"/"runtime" path construction. Every line still matching is
# listed with the reason it is correct; anything new fails the test.
_HARD_CODED = (
    re.compile(r"""/\s*["'](?:data|runtime)["']"""),                      # X / "data"
    re.compile(r"""Path\(\s*["'](?:\./)?(?:data|runtime)["'/]"""),        # Path("data"...)
    re.compile(r"""os\.path\.join\([^)\n]*["'](?:data|runtime)["']"""),   # join(..., "data", ...)
    re.compile(r"""["'](?:\./)?(?:data|runtime)/[^"'\n]*["']"""),         # "data/whatever"
)
_ALLOWED = {
    # the single definition of the two folders
    "core/config.py": (
        'RUNTIME_DIR = Path(os.getenv("MS_RUNTIME_DIR") or (BASE_DIR / "runtime"))',
        'DATA_DIR = Path(os.getenv("MS_DATA_DIR") or (BASE_DIR / "data"))',
        'default_data = _resolved(BASE_DIR / "data")',        # startup_storage_check: detects a shared default DB
        'default_runtime = _resolved(BASE_DIR / "runtime")',
        '"store_path": "data/execution.db",',                 # config default, resolved by resolve_state_path
    ),
    # core.config imports this module, so the rule is repeated (MS_DATA_DIR first)
    "core/sqlite_store.py": ('return BASE_DIR / "data" / DB_FILENAME, "default"',),
    # honour MS_DATA_DIR / MS_RUNTIME_DIR first; repo folder only as the default
    "core/analysis_store.py": (
        'data_dir = os.getenv("MS_DATA_DIR") or os.path.join(_PROJECT_ROOT, "data")',
        '"""data/news_analysis.json, or the same file under MS_DATA_DIR when set."""',
    ),
    "core/operator_audit.py": ('or (Path(__file__).resolve().parent.parent / "data")',),
    "core/state_store.py": ('or (Path(__file__).resolve().parent.parent / "runtime")',),
    "core/disaster_recovery.py": (
        'default_data_dir = Path(os.getenv("MS_DATA_DIR") or (self.project_root / "data"))',
        'data_dir = Path(os.getenv("MS_DATA_DIR") or (root / "data")).resolve()',
        'Path(os.getenv("MS_DATA_DIR") or (self.project_root / "data"))',
        # portable restore paths inside a backup archive, not filesystem paths
        'return f"runtime/{filename}"',
        '"data/trade_decisions.json",',
        '"data/trade_journal.json",',
        '"data/research_cohort_journal.json",',
        '"data/executions_crypto.json.append.jsonl",',
        '"data/executions_stocks.json.append.jsonl",',
        '"data/news_evidence_ledger.jsonl",',
        'return f"data/{self.database_path.name}"',
    ),
    # repo-relative prefixes skipped by the code scanner, not a state path
    "core/development_factory/watchdog.py": (
        'skip_prefixes = (".venv", "node_modules", "runtime/", "logs/", ".git", "tests/")',
    ),
    # read-only reference data shipped with the install
    "api/main.py": ('return os.path.join(base_dir, "data", f"us_macro_calendar_{year}.json")',),
}


def _hard_coded_lines():
    for package in ("core", "api", "agents"):
        for path in sorted((REPO / package).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO).as_posix()
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if any(pattern.search(line) for pattern in _HARD_CODED):
                    yield rel, number, stripped


def test_no_new_hard_coded_data_or_runtime_paths():
    unexpected = [
        f"{rel}:{number}: {line}"
        for rel, number, line in _hard_coded_lines()
        if line not in _ALLOWED.get(rel, ())
    ]
    assert unexpected == [], (
        "hard-coded data/runtime path — build it with core.config.data_path() / "
        "runtime_path() (or DATA_DIR / RUNTIME_DIR) so MS_DATA_DIR isolates it:\n"
        + "\n".join(unexpected)
    )


def test_hard_coded_path_scan_recognises_the_old_defects():
    """The patterns must catch every form the defect took."""
    old_lines = [
        'DB_PATH = Path(os.getenv("MS_SQLITE_DB_PATH", BASE_DIR / "data" / "market_signals.db"))',
        'REQUEST_PATH = BASE_DIR / "data" / "autonomous_cycle_request.json"',
        'return Path("data") / _DEFAULT_LEDGER_FILENAME',
        '_ENGINE_HEARTBEAT_PATH = Path(__file__).resolve().parents[1] / "data" / "agent_status_engine.json"',
        'self._history_file = Path("data/news_history.jsonl")',
        'macro_path = os.path.join(base_dir, "data", "x.json")',
        'return Path(os.getenv("MS_EXECUTION_DB_PATH", configured or "data/execution.db"))',
        "DEFAULT_WORKSPACE = Path('runtime') / 'rdagent_workspace'",
    ]
    for line in old_lines:
        assert any(pattern.search(line) for pattern in _HARD_CODED), line
    clean = [
        'cache_dir = data_path("cache_backtest")',
        'payload = response.get("data") or {}',
        'bucket = _hotlist_earnings_context.setdefault("data", {})',
        'ARCHIVE_DIR: Path = Path(DATA_DIR)',
    ]
    for line in clean:
        assert not any(pattern.search(line) for pattern in _HARD_CODED), line


# ───────────────────────── Task 3: the database path ───────────────────────

def test_database_path_follows_the_data_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("MS_SQLITE_DB_PATH", raising=False)
    monkeypatch.setenv("MS_DATA_DIR", str(tmp_path / "second-instance"))
    path, source = sqlite_store.resolve_db_path()
    assert path == tmp_path / "second-instance" / "market_signals.db"
    assert source == "MS_DATA_DIR"

    monkeypatch.setenv("MS_SQLITE_DB_PATH", str(tmp_path / "explicit.db"))
    assert sqlite_store.resolve_db_path() == (tmp_path / "explicit.db", "MS_SQLITE_DB_PATH")

    # An empty override is "not set", not the current directory.
    monkeypatch.setenv("MS_SQLITE_DB_PATH", "  ")
    assert sqlite_store.resolve_db_path()[1] == "MS_DATA_DIR"

    monkeypatch.delenv("MS_SQLITE_DB_PATH")
    monkeypatch.delenv("MS_DATA_DIR")
    path, source = sqlite_store.resolve_db_path()
    assert path == sqlite_store.BASE_DIR / "data" / "market_signals.db"
    assert source == "default"


def test_database_path_is_logged_once_when_the_store_opens(monkeypatch, tmp_path, caplog):
    db_path = tmp_path / "own" / "market_signals.db"

    class _Store(sqlite_store.SQLiteKVStore):
        def __init__(self):
            super().__init__(db_path)

    monkeypatch.setattr(sqlite_store.SQLiteKVStore, "_instance", None)
    monkeypatch.setattr(sqlite_store, "SQLiteKVStore", _Store)
    monkeypatch.setattr(sqlite_store, "DB_PATH_SOURCE", "MS_DATA_DIR")
    with caplog.at_level(logging.INFO, logger="SQLiteStore"):
        first = _Store.instance()
        second = _Store.instance()
    try:
        assert first is second
        lines = [r.getMessage() for r in caplog.records if "SQLite store opened" in r.getMessage()]
        assert len(lines) == 1, lines
        assert str(db_path) in lines[0] and "MS_DATA_DIR" in lines[0]
    finally:
        first._conn.close()
        if first._read_conn is not first._conn:
            first._read_conn.close()


def test_startup_check_logs_the_layout_and_warns_about_a_shared_default_db(monkeypatch, tmp_path, caplog):
    own_data, own_runtime = tmp_path / "data", tmp_path / "runtime"
    own_data.mkdir()
    own_runtime.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", own_data)
    monkeypatch.setattr(config, "RUNTIME_DIR", own_runtime)
    monkeypatch.setattr(sqlite_store, "DB_PATH", own_data / "market_signals.db")
    monkeypatch.setattr(sqlite_store, "DB_PATH_SOURCE", "MS_DATA_DIR")
    with caplog.at_level(logging.INFO, logger="MarketEngine"):
        report = config.startup_storage_check("api")
    assert report["shares_default_database"] is False
    assert report["sqlite_path"] == str((own_data / "market_signals.db").resolve())
    layout = [r for r in caplog.records if "Storage layout [api]" in r.getMessage()]
    assert len(layout) == 1 and layout[0].levelno == logging.INFO
    assert str(own_data.resolve()) in layout[0].getMessage()
    assert "MS_DATA_DIR" in layout[0].getMessage()

    # Own runtime folder but the default database: reported, never silent.
    caplog.clear()
    monkeypatch.setattr(sqlite_store, "DB_PATH", config.BASE_DIR / "data" / "market_signals.db")
    monkeypatch.setattr(sqlite_store, "DB_PATH_SOURCE", "default")
    with caplog.at_level(logging.INFO, logger="MarketEngine"):
        report = config.startup_storage_check("engine")
    assert report["shares_default_database"] is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "default database" in warnings[0].getMessage()


# ───────────────────────── Task 2: temp-file hygiene ───────────────────────

_OLD = 3600.0  # seconds: well past the 15-minute threshold


def _make(path: Path, *, age_seconds: float = 0.0, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


def test_sweep_removes_only_old_files_with_the_writers_own_names(tmp_path, caplog):
    data, runtime = tmp_path / "data", tmp_path / "runtime"
    old_orphans = [
        _make(data / "tmp079kbvr0.tmp", age_seconds=_OLD, size=2 * 1024 * 1024),  # mkstemp default prefix
        _make(data / "trade_decisions.a1b2c3d4.tmp", age_seconds=_OLD),
        _make(data / "strategy_research_cache.json.zz_19v9r.tmp", age_seconds=_OLD),
        _make(data / "calendar_cache_9bwix2xc.tmp", age_seconds=_OLD),
        _make(data / "analysis_store_2t5kigd5.tmp", age_seconds=_OLD),
        _make(data / "journal.flbnzcuv.tmp", age_seconds=_OLD),
        _make(data / ".sec_filings_cache.json.4321.98765.tmp", age_seconds=_OLD),
        _make(data / "telegram_delivery_status.json.4321.tmp", age_seconds=_OLD),
        _make(runtime / "capital.json.0uxj2qes.tmp", age_seconds=_OLD, size=1024 * 1024),
        _make(runtime / "signals_state.json.03blex2q.tmp", age_seconds=_OLD),
        _make(runtime / "signals_state.summary.json._0xr4xoa.tmp", age_seconds=_OLD),
        _make(runtime / ".config.json.abcd1234.migrate", age_seconds=_OLD),
    ]
    fresh = [  # a write may be in flight: younger than 15 minutes is never touched
        _make(data / "tmpabcdefgh.tmp"),
        _make(runtime / "capital.json.fresh001.tmp", age_seconds=14 * 60),
    ]
    kept = [  # old, but not a name any of the app's writers produces
        _make(data / "market_signals.db", age_seconds=_OLD),
        _make(data / "market_signals.db-wal", age_seconds=_OLD),
        _make(data / "news_backup.json", age_seconds=_OLD),
        _make(data / "news_backup.json.bak", age_seconds=_OLD),
        _make(data / "pre_earnings.json.tmp", age_seconds=_OLD),      # fixed name, reused by the next write
        _make(data / "notes.tmp", age_seconds=_OLD),
        _make(data / "tmp.tmp", age_seconds=_OLD),
        _make(data / "tmpZZZZZZZZ.tmp", age_seconds=_OLD),            # mkstemp never produces upper case
        _make(data / "tmp123.tmp", age_seconds=_OLD),
        _make(data / "template.txt", age_seconds=_OLD),
        _make(runtime / "capital.json", age_seconds=_OLD),
        _make(runtime / "config.json.lock", age_seconds=_OLD),
        _make(runtime / "signals_state.bak.json", age_seconds=_OLD),
    ]
    nested = [  # sub-folders are never entered
        _make(data / "entry_charts" / "tmp079kbvr0.tmp", age_seconds=_OLD),
        _make(runtime / "migration_backup_20260820" / "capital.json.0uxj2qes.tmp", age_seconds=_OLD),
    ]
    matching_dir = data / "tmpdirname.tmp"  # a directory with a matching name stays
    matching_dir.mkdir()
    os.utime(matching_dir, (time.time() - _OLD, time.time() - _OLD))

    with caplog.at_level(logging.INFO, logger="MarketEngine"):
        report = config.sweep_orphan_temp_files([data, runtime])

    assert [p.name for p in old_orphans if p.exists()] == []
    assert all(p.exists() for p in fresh + kept + nested)
    assert matching_dir.is_dir()
    assert report["removed"] == len(old_orphans)
    assert report["failed"] == 0
    assert report["freed_bytes"] == 3 * 1024 * 1024 + 10 * (len(old_orphans) - 2)
    assert report["freed_mb"] == 3.0
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert f"removed {len(old_orphans)} orphaned temp file(s)" in message
    assert "3.0 MB" in message

    # Idempotent: nothing left to remove, and it says so.
    again = config.sweep_orphan_temp_files([data, runtime])
    assert again["removed"] == 0 and again["freed_bytes"] == 0


def test_sweep_default_targets_are_the_data_and_runtime_folders(monkeypatch, tmp_path):
    data, runtime, other = tmp_path / "data", tmp_path / "runtime", tmp_path / "other"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    in_data = _make(data / "tmp15v15dg3.tmp", age_seconds=_OLD)
    in_runtime = _make(runtime / "state.json.k3j2h1g0.tmp", age_seconds=_OLD)
    elsewhere = _make(other / "tmp15v15dg3.tmp", age_seconds=_OLD)

    report = config.sweep_orphan_temp_files()

    assert not in_data.exists() and not in_runtime.exists()
    assert elsewhere.exists()
    assert report["removed"] == 2
    # A missing folder is not an error.
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "does-not-exist")
    assert config.sweep_orphan_temp_files()["removed"] == 0


def test_sweep_threshold_is_fifteen_minutes(tmp_path):
    assert config.ORPHAN_TEMP_MIN_AGE_SECONDS == 15 * 60
    just_inside = _make(tmp_path / "capital.json.aaaaaaaa.tmp", age_seconds=15 * 60 - 30)
    just_outside = _make(tmp_path / "capital.json.bbbbbbbb.tmp", age_seconds=15 * 60 + 30)
    config.sweep_orphan_temp_files([tmp_path])
    assert just_inside.exists()
    assert not just_outside.exists()


def test_temp_name_patterns_match_what_the_writers_really_produce(tmp_path):
    """Names come from the real writers, so the patterns cannot drift."""
    import tempfile

    produced = []
    for prefix in (None, "capital.json.", "trade_decisions.", "calendar_cache_", "analysis_store_"):
        kwargs = {"suffix": ".tmp", "dir": str(tmp_path)}
        if prefix:
            kwargs["prefix"] = prefix
        for _ in range(25):
            fd, name = tempfile.mkstemp(**kwargs)
            os.close(fd)
            produced.append(Path(name).name)
    assert all(config.is_orphan_temp_name(name) for name in produced), [
        name for name in produced if not config.is_orphan_temp_name(name)
    ]
    for name in (
        "market_signals.db", "capital.json", "config.json.lock", "news_backup.json.bak",
        "pre_earnings.json.tmp", "x.tmp", "tmp.tmp", "data.tmp.json", "tmp079kbvr0.tmp.json",
    ):
        assert not config.is_orphan_temp_name(name), name


class _Unserializable:
    pass


def _temp_leftovers(folder: Path):
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".tmp"))


@pytest.mark.parametrize("writer", [
    lambda path, data: config._write_json_atomic(path, data),
    lambda path, data: state_persistence._write_json_atomic(path, data),
])
def test_json_writers_leave_no_temp_file_when_the_write_fails(tmp_path, writer):
    target = tmp_path / "state.json"
    target.write_text('{"kept": true}', encoding="utf-8")
    with pytest.raises(TypeError):
        writer(target, {"ok": 1, "bad": _Unserializable()})
    assert _temp_leftovers(tmp_path) == []
    assert json.loads(target.read_text(encoding="utf-8")) == {"kept": True}  # old content intact


def test_writers_leave_no_temp_file_when_the_replace_fails(tmp_path, monkeypatch):
    def denied(src, dst):
        raise PermissionError("target is locked")

    monkeypatch.setattr(os, "replace", denied)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.raises(PermissionError):
        config._write_json_atomic(tmp_path / "a.json", {"a": 1})
    with pytest.raises(PermissionError):
        state_persistence._write_json_atomic(tmp_path / "b.json", {"b": 1})
    with pytest.raises(PermissionError):
        state_store._atomic_write(tmp_path / "c.json", {"c": 1})
    with pytest.raises(PermissionError):
        state_store._atomic_write_text(tmp_path / "d.json", '{"d": 1}')
    assert _temp_leftovers(tmp_path) == []


def test_writers_keep_working_and_clean_up_on_success(tmp_path):
    config._write_json_atomic(tmp_path / "a.json", {"a": 1})
    state_persistence._write_json_atomic(tmp_path / "b.json", {"b": 1})
    state_store._atomic_write(tmp_path / "c.json", {"c": 1})
    state_store._atomic_write_text(tmp_path / "d.json", '{"d": 1}')
    assert json.loads((tmp_path / "a.json").read_text(encoding="utf-8")) == {"a": 1}
    assert json.loads((tmp_path / "b.json").read_text(encoding="utf-8")) == {"b": 1}
    assert json.loads((tmp_path / "c.json").read_text(encoding="utf-8")) == {"c": 1}
    assert json.loads((tmp_path / "d.json").read_text(encoding="utf-8")) == {"d": 1}
    assert _temp_leftovers(tmp_path) == []


@pytest.mark.parametrize("discard", [config._discard_temp_file, state_store._discard_temp_file])
def test_temp_file_removal_is_retried_when_the_file_is_briefly_locked(tmp_path, monkeypatch, discard):
    """One failed unlink used to be swallowed and the temp file stayed forever."""
    victim = tmp_path / "capital.json.abcdefgh.tmp"
    victim.write_text("x", encoding="utf-8")
    real_unlink = Path.unlink
    attempts = {"n": 0}

    def flaky(self, *args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("locked by a scanner")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    assert discard(victim) is True
    assert attempts["n"] == 3
    assert not victim.exists()
    assert discard(victim) is True  # already gone
    assert discard(None) is True


def test_start_paths_run_the_storage_check_once():
    """Wired into the server lifespan start and the engine process start."""
    main_source = (REPO / "api" / "main.py").read_text(encoding="utf-8")
    engine_source = (REPO / "core" / "engine.py").read_text(encoding="utf-8")
    assert main_source.count('startup_storage_check("api")') == 1
    assert engine_source.count('startup_storage_check("engine")') == 1
    startup = main_source.split("async def startup_event_impl", 1)[1].split("\nasync def ", 1)[0]
    assert 'startup_storage_check("api")' in startup
    run_forever = engine_source.split("def run_forever(self)", 1)[1].split("\n    def ", 1)[0]
    assert 'startup_storage_check("engine")' in run_forever
    assert run_forever.index("startup_storage_check") < run_forever.index("while True")
