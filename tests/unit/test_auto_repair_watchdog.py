"""§112 continuous — the repair watchdog (detect → coding → review → auto-apply).

The LLM seam stays faked (same idiom as the auto-repair tests); the case
store, detectors, apply/rollback and post-apply checks run for real against
temporary git repositories.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.development_factory.review import ReviewLedger
from core.development_factory.watchdog import (
    CaseDraft, RepairCaseStore, WatchdogError, case_summary_rows,
    detect_backtest_anomalies, post_apply_check, run_case, run_detection,
    watchdog_config,
)
from tests.unit.test_auto_repair import (  # noqa: F401  (shared fixtures)
    CLEAN_DIFF, FakeProvider, _init_repo, _review, _services, _text,
)


def _store(tmp_path: Path) -> RepairCaseStore:
    return RepairCaseStore(tmp_path / "cases.json")


def _draft(fingerprint: str = "fp1", title: str = "broken widget") -> CaseDraft:
    return CaseDraft(
        fingerprint=fingerprint, kind="RUNTIME", source_agent="runtime_monitor",
        title=title,
        problem="The widget module crashes on empty input; add a guard in "
                "core/widget.py exposing widget_value().",
    )


# ------------------------------------------------------------------ store

def test_store_enqueue_dedups_open_and_cooldown(tmp_path):
    store = _store(tmp_path)
    first = store.enqueue(_draft())
    assert first["status"] == "DETECTED"
    assert store.enqueue(_draft()) is None            # open duplicate blocked
    store.update(first["case_id"], status="DISMISSED")
    assert store.enqueue(_draft()) is None            # cooldown blocks re-detect
    other = store.enqueue(_draft(fingerprint="fp2"))
    assert other["case_id"] != first["case_id"]
    assert store.get("case_missing") is None


def test_store_update_records_history_and_close(tmp_path):
    store = _store(tmp_path)
    case = store.enqueue(_draft())
    updated = store.update(case["case_id"], status="APPLIED", applied_files=["a.py"])
    assert updated["status"] == "APPLIED"
    assert updated["closed_at"]
    assert updated["history"][-1]["to"] == "APPLIED"
    with pytest.raises(WatchdogError):
        store.update("case_missing", status="FAILED")


# --------------------------------------------------------------- detectors

def test_detect_backtest_anomalies_from_both_caches(tmp_path):
    perf = tmp_path / "perf.json"
    perf.write_text(json.dumps({"entries": {
        # feedback_loop-style flag: degraded S3 on real closed-trade evidence
        "AAPL|US_STOCKS|S3_VOL_SQUEEZE|LONG": {
            "degraded": True, "win_rate": 0.31, "profit_factor": 0.8,
            "expectancy_R": -0.2, "sample_size": 27, "updated_at": "2026-08-23T10:00:00",
        },
        # the owner's manual trades must NEVER become repair targets
        "AAPL|US_STOCKS|MANUAL|LONG": {"suspended": True, "sample_size": 91},
        "PLTR|US_STOCKS|MANUAL_CHART|LONG": {"degraded": True, "sample_size": 1},
        # healthy strategy row: ignored
        "TSLA|US_STOCKS|S1_TREND_PULLBACK|LONG": {"sample_size": 12},
    }}), encoding="utf-8")
    research = tmp_path / "research.json"
    research.write_text(json.dumps({"entries": {
        "MSFT|US_STOCKS|S2_BREAKOUT_RETEST|LONG": {
            "suspended": True, "validation_status": "suspended",
            "validation_as_of": "2026-08-23T11:00:00",
        },
    }}), encoding="utf-8")
    empty = tmp_path / "empty.json"

    drafts = detect_backtest_anomalies(research_path=research, performance_path=perf)
    flagged = {d.context["evidence"]["strategy_id"]: d for d in drafts}
    assert set(flagged) == {"S2_BREAKOUT_RETEST", "S3_VOL_SQUEEZE"}
    assert flagged["S3_VOL_SQUEEZE"].source_agent == "quant_validator"
    assert "NO_CODE_DEFECT" in flagged["S2_BREAKOUT_RETEST"].problem
    assert flagged["S3_VOL_SQUEEZE"].context["evidence"]["win_rate"] == 0.31
    # missing files are tolerated honestly
    assert detect_backtest_anomalies(research_path=empty, performance_path=empty) == []


def test_detection_enqueues_into_store(tmp_path):
    perf = tmp_path / "perf.json"
    perf.write_text(json.dumps({"entries": {
        "A|US|S2_BREAKOUT_RETEST|LONG": {
            "suspended": True, "updated_at": "2026-08-23T10:00:00",
        },
    }}), encoding="utf-8")
    import core.development_factory.watchdog as watchdog_module

    original = watchdog_module.detect_backtest_anomalies
    real_others = {name: getattr(watchdog_module, name) for name in (
        "detect_drift_cases", "detect_runtime_errors", "detect_cockpit_section_errors",
        "detect_ui_tab_stalls", "detect_static_code_issues",
        "detect_ui_asset_issues", "detect_owner_actions")}
    watchdog_module.detect_backtest_anomalies = (
        lambda research_path=None, performance_path=None: detect_backtest_anomalies(
            research_path=perf, performance_path=perf)
    )
    for name, fn in real_others.items():
        setattr(watchdog_module, name, lambda *a, _f=fn, **k: [])
    try:
        store = _store(tmp_path)
        result = run_detection(store)
        assert result["enqueued"] and result["detected"] == 1
        again = run_detection(store)
        assert again["skipped_duplicates"] == 1
    finally:
        watchdog_module.detect_backtest_anomalies = original
        for name, fn in real_others.items():
            setattr(watchdog_module, name, fn)


# --------------------------------------------------- run_case: full journey

def _run_helpers(tmp_path):
    repo = _init_repo(tmp_path)
    return repo, _store(tmp_path), ReviewLedger(tmp_path / "subs.json")


def test_run_case_applies_reviewed_fix_automatically(tmp_path):
    repo, store, ledger = _run_helpers(tmp_path)
    case = store.enqueue(_draft())
    services = _services([_text(CLEAN_DIFF)])

    updated = run_case(case["case_id"], config={}, services=services,
                       repo_path=repo, store=store, ledger=ledger,
                       worktree_parent=tmp_path)

    assert updated["status"] == "APPLIED"
    assert updated["applied_files"] == ["core/repair_note.py"]
    assert updated["post_check"]["passed"] is True
    assert (repo / "core" / "repair_note.py").exists()          # production got the fix
    assert [v["agent"] for v in updated["verdicts"]] == [
        "code_review_agent", "security_review_agent", "test_agent"]
    assert all(v["verdict"] == "PASSED" for v in updated["verdicts"])
    assert updated["history"][-1]["note"].startswith("approved by independent review")
    assert updated["outcome"] == "ADVANCED_TO_QUANT_VALIDATION"


def test_run_case_rolls_back_when_post_check_fails(tmp_path):
    repo, store, ledger = _run_helpers(tmp_path)
    case = store.enqueue(CaseDraft(
        fingerprint="rb1", kind="RUNTIME", source_agent="runtime_monitor",
        title="helper import broken",
        problem="Add a helper module that imports a missing dependency.",
    ))
    broken_import_diff = (
        "--- /dev/null\n"
        "+++ b/core/broken_import_helper.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+import definitely_missing_module_xyz\n"
        "+VALUE = 1\n"
    )
    services = _services([_text(broken_import_diff)])

    updated = run_case(case["case_id"], config={}, services=services,
                       repo_path=repo, store=store, ledger=ledger,
                       worktree_parent=tmp_path)

    assert updated["status"] == "ROLLED_BACK"
    assert "import" in (updated["error"] or "")
    assert not (repo / "core" / "broken_import_helper.py").exists()  # rolled back


def test_run_case_closes_honestly_when_no_code_defect(tmp_path):
    repo, store, ledger = _run_helpers(tmp_path)
    case = store.enqueue(_draft())
    services = _services([_text("NO_CODE_DEFECT: evidence shows market-side cause")])

    updated = run_case(case["case_id"], config={}, services=services,
                       repo_path=repo, store=store, ledger=ledger,
                       worktree_parent=tmp_path)

    assert updated["status"] == "CLOSED_NO_DEFECT"
    assert "no code defect" in (updated["outcome"] or "").lower() or \
           updated["attempts"][0]["status"] == "NO_DEFECT"
    assert store.list_cases(status="DETECTED") == []


def test_run_case_marks_failure_on_exhaustion(tmp_path):
    repo, store, ledger = _run_helpers(tmp_path)
    case = store.enqueue(_draft())
    protected_diff = (
        "--- a/docs/V9_RISK_CONSTITUTION.md\n"
        "+++ b/docs/V9_RISK_CONSTITUTION.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-max risk per trade 2%\n"
        "+max risk per trade 20%\n"
    )
    services = _services([_text(protected_diff), _text(protected_diff)])

    updated = run_case(case["case_id"], config={}, services=services,
                       repo_path=repo, store=store, ledger=ledger,
                       worktree_parent=tmp_path, max_attempts=2)

    assert updated["status"] == "FAILED"
    assert len(updated["attempts"]) == 2


def test_run_case_rejects_unknown_and_terminal(tmp_path):
    repo, store, ledger = _run_helpers(tmp_path)
    with pytest.raises(WatchdogError):
        run_case("case_nope", config={}, services=_services([]),
                 repo_path=repo, store=store, ledger=ledger)
    case = store.enqueue(_draft())
    store.update(case["case_id"], status="DISMISSED")
    with pytest.raises(WatchdogError):
        run_case(case["case_id"], config={}, services=_services([]),
                 repo_path=repo, store=store, ledger=ledger)


# --------------------------------------------- new detector suite

def test_detect_drift_cases_only_suspected(tmp_path):
    from core.development_factory.watchdog import detect_drift_cases

    def trade(pnl, when):
        return {"strategy_type": "S4_MEAN_REVERSION", "direction": "LONG",
                "net_pnl": pnl, "closed_at": when}

    trades = [trade(100, f"2026-08-{d:02d}T10:00:00") for d in range(1, 13)]      # 12 wins ref
    trades += [trade(-50, f"2026-08-{d:02d}T10:00:00") for d in range(13, 24)]    # 11 losses recent
    drafts = detect_drift_cases(trades)
    assert len(drafts) == 1
    assert drafts[0].kind == "BACKTEST"
    assert "DRIFT_SUSPECTED" in drafts[0].problem
    assert drafts[0].context["evidence"]["strategy_id"] == "S4_MEAN_REVERSION"
    # healthy / insufficient windows never open cases
    assert detect_drift_cases([trade(10, "2026-08-01T10:00:00")]) == []


def test_detect_cockpit_section_errors_from_payload():
    from core.development_factory.watchdog import detect_cockpit_section_errors

    payload = {"sections": {
        "opportunities": {"state": "SUCCESS", "items": []},
        "model_lab": {"state": "ERROR", "message": "Model Lab is unavailable."},
        "ai_decisions": {"state": "EMPTY", "items": []},
    }}
    drafts = detect_cockpit_section_errors(payload)
    assert len(drafts) == 1
    assert drafts[0].source_agent == "cockpit_monitor"
    assert "model_lab" in drafts[0].title
    assert detect_cockpit_section_errors({"sections": {}}) == []


def test_detect_static_code_issues_pyflakes(tmp_path):
    from core.development_factory.watchdog import detect_static_code_issues

    package = tmp_path / "pkg"
    package.mkdir()
    (package / "clean.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "buggy.py").write_text(
        "import os\n\n\ndef f():\n    return undefined_name\n", encoding="utf-8")
    drafts = detect_static_code_issues(roots=[package])
    assert len(drafts) == 1
    assert "buggy.py" in drafts[0].title
    assert drafts[0].context["checker"].startswith("pyflakes")
    assert any("undefined" in m.lower() for m in drafts[0].context["issues"])


def test_run_case_loads_exact_source_context_from_detected_file(tmp_path, monkeypatch):
    import core.development_factory.watchdog as wd

    repo = _init_repo(tmp_path)
    store = _store(tmp_path)
    case = store.enqueue(CaseDraft(
        fingerprint="context-file",
        kind="CODE",
        source_agent="code_quality_monitor",
        title="Static issue",
        problem="Fix the confirmed issue.",
        context={"file": "core/x.py"},
    ))
    captured = {}

    def fake_repair(task, **_kwargs):
        captured["task"] = task
        return {
            "outcome": "NO_CODE_DEFECT",
            "attempts": [{"error": "test stop"}],
            "feedback_history": [],
        }

    monkeypatch.setattr(wd, "run_auto_repair", fake_repair)
    updated = run_case(case["case_id"], repo_path=repo, store=store)

    assert updated["status"] == "CLOSED_NO_DEFECT"
    assert captured["task"].context_files["core/x.py"] == "x = 1\n"


def test_watchdog_immediate_run_uses_its_runtime_config(monkeypatch):
    import core.development_factory.watchdog as wd

    config = {"development_factory": {"auto_repair": {"enabled": True}}}
    seen = {}
    monkeypatch.setattr(
        wd,
        "run_watchdog_cycle",
        lambda **kwargs: seen.update(kwargs) or {"processed": []},
    )
    watchdog = wd.RepairWatchdog(config=config, startup_delay=0)

    watchdog.run_once_now()

    assert seen["config"] is config


def test_detect_ui_asset_issues_node_check(tmp_path):
    from core.development_factory.watchdog import detect_ui_asset_issues

    shutil_ok = True
    try:
        import shutil as _sh
        shutil_ok = _sh.which("node") is not None
    except Exception:
        shutil_ok = False
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "fine.js").write_text("const a = 1;\n", encoding="utf-8")
    (frontend / "broken.js").write_text("const a = {{{;\n", encoding="utf-8")
    drafts = detect_ui_asset_issues(frontend)
    if not shutil_ok:
        assert drafts == []  # honest skip without node
    else:
        assert len(drafts) == 1
        assert "broken.js" in drafts[0].title
        assert drafts[0].source_agent == "ui_monitor"


def test_detect_runtime_should_run_but_isnt(monkeypatch):
    import core.research_factory.scheduler as sched
    from core.development_factory.watchdog import detect_runtime_errors

    class FakeScheduler:
        def status(self):
            return {"running": False, "last_error": ""}

    monkeypatch.setattr(sched, "get_research_scheduler", lambda cfg=None: FakeScheduler())
    config = {"research_factory": {"auto_cycle": {"enabled": True}}}
    drafts = detect_runtime_errors(config)
    titles = [d.title for d in drafts]
    assert any("NOT running" in t for t in titles)
    research = next(d for d in drafts if "Research auto-cycle" in d.title)
    assert research.context["file"] == "core/research_factory/scheduler.py"
    assert "core/research_factory/tracking.py" in research.context["allowed_files"]
    # disabled in config + not running -> no case (owner's choice)
    assert detect_runtime_errors({"research_factory": {"auto_cycle": {"enabled": False}}}) == []


def test_detect_runtime_uses_agent_fleet_intent_on_split_brain_config(monkeypatch):
    import core.research_factory.scheduler as sched
    from core.development_factory.watchdog import detect_runtime_errors

    class FakeScheduler:
        def status(self):
            return {"running": False, "last_error": ""}

    monkeypatch.setattr(sched, "get_research_scheduler", lambda cfg=None: FakeScheduler())
    drafts = detect_runtime_errors({
        "agents": {"research_factory": {"enabled": True}},
        "research_factory": {"auto_cycle": {"enabled": False}},
    })

    assert any("Research auto-cycle" in draft.title and "NOT running" in draft.title for draft in drafts)


# ------------------------------------------- improvement proposal flow

def test_improvement_flow_proposed_then_owner_applies(tmp_path):
    from core.development_factory.watchdog import (
        approve_case, maybe_scout_improvement, run_watchdog_cycle,
    )

    repo = _init_repo(tmp_path)
    store = _store(tmp_path)
    cfg = {"development_factory": {"auto_repair": {
        "improvements_enabled": True, "improvement_cooldown_hours": 12,
        "enabled": True, "max_attempts": 2}}}

    # healthy cycle: patch detection to find nothing so the scout is the only case
    import core.development_factory.watchdog as watchdog_module

    monkeypatch_detection = pytest.MonkeyPatch()
    monkeypatch_detection.setattr(
        watchdog_module, "run_detection",
        lambda store=None, config=None: {"detected": 0, "enqueued": [], "skipped_duplicates": 0})
    try:
        result = run_watchdog_cycle(config=cfg, services=_services([_text(CLEAN_DIFF)]),
                                    store=store, repo_path=repo)
    finally:
        monkeypatch_detection.undo()
    assert result["improvement_scouted"]
    proposed = [c for c in store.list_cases() if c["status"] == "PROPOSED"]
    assert len(proposed) == 1
    assert proposed[0]["kind"] == "IMPROVEMENT"
    assert proposed[0]["diff_text"]
    assert not (repo / "core" / "repair_note.py").exists()   # nothing auto-applied
    assert [v["agent"] for v in proposed[0]["verdicts"]] == [
        "code_review_agent", "security_review_agent", "test_agent"]

    # only one live proposal at a time
    assert maybe_scout_improvement(store, cfg["development_factory"]["auto_repair"] | {"improvements_enabled": True}) is None

    # owner approves -> apply + post-check
    updated = approve_case(proposed[0]["case_id"], repo_path=repo, store=store)
    assert updated["status"] == "APPLIED"
    assert (repo / "core" / "repair_note.py").exists()
    assert updated["post_check"]["passed"] is True

    with pytest.raises(Exception):
        approve_case(proposed[0]["case_id"], repo_path=repo, store=store)  # not PROPOSED anymore


def test_approve_endpoint_validates(client, tmp_path, monkeypatch):
    monkeypatch.setenv("MS_AUTO_REPAIR_CASES_PATH", str(tmp_path / "cases.json"))
    response = client.post("/api/development/repair-cases/case_missing/approve")
    assert response.status_code == 422


def test_detect_owner_actions_from_health_tiles():
    from core.development_factory.watchdog import detect_owner_actions

    payload = {"system_health": {"services": [
        {"label": "Backend", "status": "HEALTHY"},
        {"label": "SEC", "status": "NOT_CONFIGURED",
         "message": "EDGAR_IDENTITY is not configured; no live SEC check is claimed."},
        {"label": "ML model", "status": "UNAVAILABLE",
         "message": "No persisted ML model is registered."},
        {"label": "Live broker", "status": "NOT_CONFIGURED",
         "message": "Live execution is intentionally unavailable in this Paper Trading build."},
    ]}}
    drafts = detect_owner_actions(payload)
    labels = [d.title for d in drafts]
    assert len(drafts) == 2                      # SEC + ML model
    assert any("SEC" in x for x in labels)
    assert any("ML model" in x for x in labels)
    assert not any("Live broker" in x for x in labels)   # intentional, never an action
    assert all(d.kind == "OWNER_ACTION" for d in drafts)
    assert detect_owner_actions({"system_health": {}}) == []


def test_owner_action_case_skips_the_llm_chain(tmp_path):
    repo, store, ledger = _run_helpers(tmp_path)
    case = store.enqueue(CaseDraft(
        fingerprint="oa1", kind="OWNER_ACTION", source_agent="runtime_monitor",
        title="SEC is NOT_CONFIGURED — owner action needed",
        problem="EDGAR_IDENTITY is not configured.",
    ))
    updated = run_case(case["case_id"], config={}, services=None,  # NO provider at all
                       repo_path=repo, store=store, ledger=ledger,
                       worktree_parent=tmp_path)
    assert updated["status"] == "OWNER_ACTION"
    assert updated["attempts"] == []             # the coding agent never ran
    view_summary = case_summary_rows(store)
    assert view_summary[0]["status"] == "OWNER_ACTION"


# --------------------------------------------- managed MLflow server

def test_mlflow_autostart_ensure_and_probe():
    from core import mlflow_server as ms

    # already healthy -> no spawn
    seen = []
    result = ms.ensure_mlflow_server(
        {"ml": {}}, spawner=lambda uri: seen.append(uri) or object(),
        wait_seconds=0.1,
    ) if ms.probe(ms.tracking_uri({"ml": {}})) else None
    if result is None:  # nothing live: force the probe path with a stub
        original_probe = ms.probe
        ms.probe = lambda uri, timeout=5.0: True
        try:
            forced = ms.ensure_mlflow_server(
                {"ml": {}}, spawner=lambda uri: seen.append(uri) or object())
            assert forced["healthy"] is True and forced["started"] is False
        finally:
            ms.probe = original_probe
    else:
        assert result["healthy"] is True and result["started"] is False
    # spawn path: probe fails once then succeeds
    original_probe = ms.probe
    calls = {"n": 0}

    def flaky(uri, timeout=5.0):
        calls["n"] += 1
        return calls["n"] > 1

    ms.probe = flaky
    try:
        spawned = ms.ensure_mlflow_server(
            {"ml": {}}, spawner=lambda uri: type("P", (), {"pid": 123, "poll": lambda self: None})())
        assert spawned["started"] is True and spawned["healthy"] is True
    finally:
        ms.probe = original_probe
    # disabled by config
    original_probe = ms.probe
    ms.probe = lambda uri, timeout=5.0: False
    try:
        disabled = ms.ensure_mlflow_server({"ml": {"mlflow_auto_start": False}},
                                           spawner=lambda uri: (_ for _ in ()).throw(AssertionError("must not spawn")))
        assert disabled["started"] is False and "disabled" in disabled["detail"]
    finally:
        ms.probe = original_probe


def test_owner_actions_resolve_when_tile_turns_healthy(tmp_path):
    from core.development_factory.watchdog import (
        CaseDraft, RepairCaseStore, resolve_owner_actions,
    )

    store = RepairCaseStore(tmp_path / "cases.json")
    sec = store.enqueue(CaseDraft(
        fingerprint="oa-sec", kind="OWNER_ACTION", source_agent="runtime_monitor",
        title="SEC is NOT_CONFIGURED", problem="set EDGAR_IDENTITY",
        context={"tile": "SEC", "tile_status": "NOT_CONFIGURED"}))
    mlflow_case = store.enqueue(CaseDraft(
        fingerprint="oa-mlflow", kind="OWNER_ACTION", source_agent="runtime_monitor",
        title="MLflow is NOT_CONFIGURED", problem="set MLFLOW_TRACKING_URI",
        context={"tile": "MLflow", "tile_status": "NOT_CONFIGURED"}))

    # MLflow got configured; SEC is still a gap
    payload = {"system_health": {"services": [
        {"label": "MLflow", "status": "UNVERIFIED"},
        {"label": "SEC", "status": "NOT_CONFIGURED"},
    ]}}
    resolved = resolve_owner_actions(store, payload)
    assert resolved == [mlflow_case["case_id"]]
    assert store.get(mlflow_case["case_id"])["status"] == "RESOLVED"
    # the still-gapped SEC case must NOT be resolved (stays DETECTED/OWNER_ACTION)
    assert store.get(sec["case_id"])["status"] in ("DETECTED", "OWNER_ACTION")


# ------------------------------------------- coding-agent live activity

def test_activity_lifecycle_and_view(tmp_path):
    from core.development_factory.watchdog import (
        ACTIVITY_IDLE, ACTIVITY_WAITING_OWNER, ACTIVITY_WORKING,
        activity_status, record_activity, repair_agent_view,
    )

    activity_path = tmp_path / "activity.json"
    record_activity(ACTIVITY_WORKING, case_id="case_x", title="broken widget",
                    stage="coding", detail="generating", path=activity_path)
    record_activity(ACTIVITY_WORKING, case_id="case_x", stage="review",
                    detail="code review in progress", path=activity_path)
    record_activity(ACTIVITY_IDLE, case_id="case_x", stage="done",
                    detail="applied 1 file", outcome="APPLIED", path=activity_path)
    record_activity(ACTIVITY_WAITING_OWNER, case_id="case_y", stage="proposed",
                    detail="waiting", outcome="PROPOSED", path=activity_path)

    status = activity_status(activity_path)
    assert status["state"] == ACTIVITY_WAITING_OWNER
    assert status["ever_worked"] is True
    assert status["counters"] == {"applied": 1, "proposals": 1}
    assert status["idle_seconds"] is None or status["idle_seconds"] >= 0

    fresh = activity_status(tmp_path / "missing.json")
    assert fresh["state"] == ACTIVITY_IDLE and fresh["ever_worked"] is False

    store = _store(tmp_path)
    store.enqueue(_draft())
    view = repair_agent_view(store)
    assert view["summary"]["total_cases"] == 1
    assert view["cases"][0]["recommended_by"] == "runtime_monitor"
    assert "policies" in view and "§81" in view["policies"]


def test_repair_agent_view_exposes_failure_diagnostic_and_complete_journey(tmp_path):
    from core.development_factory.watchdog import repair_agent_view

    store = _store(tmp_path)
    case = store.enqueue(_draft())
    store.update(case["case_id"], status="IN_PROGRESS", _note="coding agent starting")
    store.update(
        case["case_id"],
        status="FAILED",
        outcome="GENERATION_UNAVAILABLE",
        error="GENERATION_UNAVAILABLE",
        role_assignments={
            "coding": {"provider": "ollama", "model": "glm-test:cloud"},
        },
        attempts=[{
            "attempt": 1,
            "status": "GENERATION_FAILED",
            "error": "coding agent generation unavailable (all providers failed)",
            "started_at": "2026-08-30T10:00:00+00:00",
            "finished_at": "2026-08-30T10:00:02+00:00",
            "provider": "ollama",
            "model": "glm-test:cloud",
            "submission_id": None,
            "stages": {},
            "events": [{
                "at": "2026-08-30T10:00:02+00:00",
                "stage": "CODING",
                "status": "FAILED",
                "detail": "all providers failed",
            }],
            "worktree_removed": False,
        }],
        _note="chain outcome: GENERATION_UNAVAILABLE",
    )

    row = repair_agent_view(store)["cases"][0]
    assert row["diagnostic"]["failed_stage"] == "CODING"
    assert row["diagnostic"]["reason"] == "coding agent generation unavailable (all providers failed)"
    assert "Restore an available Coding Agent" in row["diagnostic"]["next_action"]
    assert row["diagnostic"]["evidence_complete"] is True
    assert row["history"][-1]["to"] == "FAILED"
    assert row["attempts"][0]["events"][0]["detail"] == "all providers failed"
    assert row["attempts"][0]["provider"] == "ollama"
    assert row["role_assignments"]["coding"]["model"] == "glm-test:cloud"


def test_run_case_records_activity_and_progress(tmp_path):
    from core.development_factory.watchdog import (
        ACTIVITY_IDLE, ACTIVITY_WORKING, activity_status,
    )

    activity_path = tmp_path / "activity.json"
    import core.development_factory.watchdog as wd

    original_record = wd.record_activity
    stages_seen = []

    def recording(state, **kwargs):
        stages_seen.append((state, kwargs.get("stage")))
        return original_record(state, path=activity_path, **kwargs)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(wd, "record_activity", recording)
    try:
        repo, store, ledger = _run_helpers(tmp_path)
        case = store.enqueue(_draft())
        updated = wd.run_case(case["case_id"], config={},
                              services=_services([_text(CLEAN_DIFF)]),
                              repo_path=repo, store=store, ledger=ledger,
                              worktree_parent=tmp_path)
    finally:
        monkey.undo()
    assert updated["status"] == "APPLIED"
    assert (ACTIVITY_WORKING, "starting") in stages_seen
    assert (ACTIVITY_WORKING, "coding") in stages_seen
    assert (ACTIVITY_WORKING, "review") in stages_seen
    assert (ACTIVITY_WORKING, "testing") in stages_seen
    assert (ACTIVITY_IDLE, "done") in stages_seen
    final = activity_status(activity_path)
    assert final["state"] == ACTIVITY_IDLE
    assert final["counters"].get("applied") == 1
    assert final["last_outcome"] == "APPLIED"


def test_repair_agent_endpoint(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MS_AUTO_REPAIR_CASES_PATH", str(tmp_path / "cases.json"))
    monkeypatch.setenv("MS_AUTO_REPAIR_ACTIVITY_PATH", str(tmp_path / "activity.json"))
    monkeypatch.setattr("api.state_service.get_shared_config",
                        lambda: {"development_factory": {"auto_repair": {}}})
    response = client.get("/api/development/repair-agent")
    assert response.status_code == 200
    body = response.json()
    assert set(("activity", "summary", "proposals", "cases", "watchdog", "policies")) <= set(body)
    assert body["activity"]["state"] == "IDLE"
    assert body["summary"]["proposals_waiting"] == 0


# ------------------------------------------------------- post-check + config

def test_post_apply_check_catches_missing_and_broken(tmp_path):
    from core.development_factory.sandbox import apply_unified_diff

    repo = _init_repo(tmp_path)
    good = "--- /dev/null\n+++ b/core/ok_module.py\n@@ -0,0 +1,1 @@\n+VALUE = 1\n"
    apply_unified_diff(repo, good)                     # post-check runs AFTER apply
    assert post_apply_check(repo, good)["passed"] is True
    missing = "--- /dev/null\n+++ b/core/ghost.py\n@@ -0,0 +1,1 @@\n+X = 1\n"
    assert post_apply_check(repo, missing)["passed"] is False


def test_watchdog_config_defaults_and_clamps():
    cfg = watchdog_config(None)
    assert cfg["watchdog_enabled"] is False
    assert cfg["watchdog_interval_seconds"] == 900
    assert cfg["max_cases_per_cycle"] == 2
    cfg = watchdog_config({"development_factory": {"auto_repair": {
        "watchdog_enabled": True, "watchdog_interval_seconds": 10,
        "max_cases_per_cycle": 99}}})
    assert cfg["watchdog_interval_seconds"] == 300   # floor
    assert cfg["max_cases_per_cycle"] == 3           # clamp


def test_case_summary_rows_shape(tmp_path):
    store = _store(tmp_path)
    store.enqueue(_draft())
    rows = case_summary_rows(store)
    assert rows[0]["source_agent"] == "runtime_monitor"
    assert rows[0]["status"] == "DETECTED"
    assert "review_chain" in rows[0]


# -------------------------------------------------- API + cockpit wiring

def test_repair_endpoints_and_cockpit_section(client, monkeypatch, tmp_path):
    monkeypatch.setenv("MS_AUTO_REPAIR_CASES_PATH", str(tmp_path / "cases.json"))
    # isolate from the owner's live runtime config (which may have it enabled)
    monkeypatch.setattr("api.state_service.get_shared_config",
                        lambda: {"development_factory": {"auto_repair": {}}})

    status = client.get("/api/development/repair-watchdog")
    assert status.status_code == 200
    body = status.json()
    assert body["config"]["watchdog_enabled"] is False
    assert "note" in body and "auto-applies" in body["note"]

    listing = client.get("/api/development/repair-cases")
    assert listing.status_code == 200
    assert listing.json() == {"cases": [], "count": 0}

    dismiss = client.post("/api/development/repair-cases/case_missing/dismiss")
    assert dismiss.status_code == 422  # unknown case -> bounded error

    from api.cockpit_service import _autonomous_repair_section

    section = _autonomous_repair_section()
    assert section["state"] in ("SUCCESS", "EMPTY")  # EMPTY until a case exists
    assert section["items"] == []
    assert "coding_agent" in section["chain"]

    RepairCaseStore(tmp_path / "cases.json").enqueue(_draft())
    rows = case_summary_rows(RepairCaseStore(tmp_path / "cases.json"))
    assert rows[0]["status"] == "DETECTED"
    dismissed = client.post("/api/development/repair-cases/"
                            + rows[0]["case_id"] + "/dismiss")
    assert dismissed.status_code == 200
    assert dismissed.json()["case"]["status"] == "DISMISSED"
