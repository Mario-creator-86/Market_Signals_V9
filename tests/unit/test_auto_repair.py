"""Phase 15+ §112 — the coding-agent self-repair loop (unit tests, no network).

The LLM seam is faked via injectable providers (same idiom as the §44 critic
tests): generation, review verdicts and the deterministic TESTS stage are all
scripted, while the policy guard, sandbox worktrees and review ledger run for
real against a temporary git repository.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.ai.provider import (
    AIProvider, AIResponse, ProviderCapabilities, Usage,
)
from core.development_factory.auto_repair import (
    AutoRepairError, RepairTask, auto_repair_config, build_task_from_experiment,
    extract_diff, read_context_files, repair_scope_findings, run_auto_repair,
)
from core.development_factory.review import ReviewLedger
from core.development_factory.sandbox import worktree_status

# ------------------------------------------------------------------ fixtures

CLEAN_DIFF = """--- /dev/null
+++ b/core/repair_note.py
@@ -0,0 +1,2 @@
+NOTE = "auto repair proof"
+VALUE = 42
"""

UNFINISHED_DIFF = """--- /dev/null
+++ b/core/repair_note.py
@@ -0,0 +1,1 @@
+NOTE = "v1 unfinished stub"
"""

FIXED_DIFF = """--- /dev/null
+++ b/core/repair_note.py
@@ -0,0 +1,2 @@
+NOTE = "v2 implements the required note"
+VALUE = 42
"""

SYNTAX_ERROR_DIFF = """--- /dev/null
+++ b/core/broken.py
@@ -0,0 +1,1 @@
+def broken(:
"""

PROTECTED_DIFF = """--- a/docs/V9_RISK_CONSTITUTION.md
+++ b/docs/V9_RISK_CONSTITUTION.md
@@ -1,1 +1,1 @@
-max risk per trade 2%
+max risk per trade 20%
"""


def test_repair_scope_rejects_unanchored_top_level_module(tmp_path):
    repo = _init_repo(tmp_path)
    task = RepairTask(
        experiment_id="scope-1",
        problem="Research cycle fails in MLflow tracking",
        context={"allowed_files": ["core/research_factory/tracking.py"]},
    )
    diff = """--- /dev/null
+++ b/research_cycle.py
@@ -0,0 +1,1 @@
+PATCHED = True
"""

    findings = repair_scope_findings(task, diff, repo)

    assert any("top-level module" in finding for finding in findings)
    assert any("does not touch any incident source file" in finding for finding in findings)


def test_repair_scope_requires_and_accepts_incident_source(tmp_path):
    repo = _init_repo(tmp_path)
    target = repo / "core" / "research_factory" / "tracking.py"
    target.parent.mkdir(parents=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    task = RepairTask(
        experiment_id="scope-2",
        problem="Tracking failure",
        context={"allowed_files": ["core/research_factory/tracking.py"]},
    )
    unrelated = """--- /dev/null
+++ b/core/unrelated.py
@@ -0,0 +1,1 @@
+VALUE = 2
"""
    anchored = """--- a/core/research_factory/tracking.py
+++ b/core/research_factory/tracking.py
@@ -1,1 +1,1 @@
-VALUE = 1
+VALUE = 2
"""

    assert repair_scope_findings(task, unrelated, repo)
    assert repair_scope_findings(task, anchored, repo) == []


class FakeProvider(AIProvider):
    """Scripted provider: entries are AIResponse or callable(prompt, system) -> AIResponse."""

    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def generate(self, prompt, *, model=None, system="", tools=None, **opts):
        self.prompts.append(prompt)
        self.systems.append(system)
        if not self.script:
            raise AssertionError("FakeProvider script exhausted")
        step = self.script.pop(0)
        if callable(step):
            step = step(prompt, system)
        return step

    # -- unused surface -----------------------------------------------------
    def list_models(self):
        return ["fake-model"]

    def health_check(self, *, force=False):
        return True

    def capabilities(self):
        return ProviderCapabilities()

    def generate_structured(self, prompt, schema, *, model=None, system="", **opts):
        return self.generate(prompt, model=model, system=system)

    def tool_call(self, prompt, tools, *, model=None, system="", **opts):
        return self.generate(prompt, model=model, system=system)

    def stream(self, prompt, *, model=None, system="", **opts):
        yield AIResponse(text="")

    def usage(self):
        return Usage()


def _text(text: str) -> AIResponse:
    return AIResponse(text=text, model="fake-model")


def _review(verdict: str, findings=()) -> AIResponse:
    import json

    return _text(json.dumps({"verdict": verdict, "findings": list(findings),
                             "rationale": f"scripted {verdict}"}))


def _init_repo(tmp_path: Path) -> Path:
    repo = Path(tmp_path) / "repo"
    repo.mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True, timeout=30)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "core").mkdir()
    (repo / "docs").mkdir()
    (repo / "core" / "x.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "docs" / "V9_RISK_CONSTITUTION.md").write_text("max risk per trade 2%\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    return repo


def _task() -> RepairTask:
    return RepairTask(experiment_id="EXP-TEST0001", problem="Add the repair note module",
                      target_hint="core/")


def _services(coding_script, review_script=None, security_script=None):
    return {
        "coding_provider": FakeProvider(coding_script),
        "coding_model": "fake-model",
        "code_review_provider": FakeProvider(review_script or [_review("PASSED")]),
        "code_review_model": "fake-review-model",
        "security_review_provider": FakeProvider(security_script or [_review("PASSED")]),
        "security_review_model": "fake-security-model",
    }


# ------------------------------------------------------------ extract_diff

def test_extract_diff_strips_fences_and_prose():
    fenced = "Here you go:\n```diff\n" + CLEAN_DIFF + "\n```"
    assert extract_diff(fenced) == CLEAN_DIFF


def test_extract_diff_rejects_garbage():
    assert extract_diff("I could not produce a diff, sorry.") == ""
    assert extract_diff("") == ""


# ------------------------------------------------------- repo file context

def test_context_files_reach_the_coding_prompt(tmp_path):
    repo = _init_repo(tmp_path)
    task = RepairTask(experiment_id="EXP-CTX1", problem="edit core/x.py",
                      context_files={"core/x.py": "x = 1\n"})
    coding = FakeProvider([_text(CLEAN_DIFF)])
    result = run_auto_repair(task, config={},
                             services=_services([]) | {"coding_provider": coding},
                             repo_path=repo, ledger=ReviewLedger(tmp_path / "s.json"),
                             worktree_parent=tmp_path)
    prompt = coding.prompts[0]
    assert "----- core/x.py -----" in prompt
    assert "x = 1" in prompt
    assert "byte-for-byte" in prompt


def test_read_context_files_bounded_and_repo_safe(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "core" / "big.py").write_text("y = 2\n" * 100, encoding="utf-8")
    excerpts = read_context_files(repo, ["core/x.py", "core/big.py", "../outside.py"],
                                  max_total=50)
    assert "core/x.py" in excerpts and excerpts["core/x.py"] == "x = 1\n"
    assert len(excerpts.get("core/big.py", "")) <= 50
    assert "../outside.py" not in excerpts
    assert read_context_files(repo, ["core/missing.py"]) == {}


# ------------------------------------------------------------- happy path

def test_happy_path_advances_to_quant_validation(tmp_path):
    repo = _init_repo(tmp_path)
    ledger = ReviewLedger(tmp_path / "subs.json")
    services = _services([_text(CLEAN_DIFF)])

    result = run_auto_repair(_task(), config={}, services=services,
                             max_attempts=3, repo_path=repo, ledger=ledger,
                             worktree_parent=tmp_path)

    assert result["outcome"] == "ADVANCED_TO_QUANT_VALIDATION"
    assert result["attempts_used"] == 1
    attempt = result["attempts"][0]
    assert attempt["stages"] == {"CODE_REVIEW": "PASSED",
                                 "SECURITY_REVIEW": "PASSED", "TESTS": "PASSED"}
    assert attempt["status"] == "OPEN"
    assert attempt["started_at"] and attempt["finished_at"]
    assert {event["stage"] for event in attempt["events"]} >= {
        "CODING", "SCOPE_GUARD", "POLICY_GUARD", "SANDBOX",
        "CODE_REVIEW", "SECURITY_REVIEW", "TESTS", "REVIEW_CHAIN",
    }
    assert result["role_assignments"]["coding"]["provider"]
    submission = ledger.get(attempt["submission_id"])
    assert submission.status == "OPEN"  # QUANT_VALIDATION/E2E honestly unrecorded
    assert {r["stage"] for r in submission.reviews} == {"CODE_REVIEW", "SECURITY_REVIEW", "TESTS"}
    # sandbox artifact survives for the Phase 16/17 actors
    assert worktree_status(repo, "EXP-TEST0001-R1")["exists"] is True
    # production tree untouched (§80)
    assert not (repo / "core" / "repair_note.py").exists()
    assert (Path(attempt["worktree"]) / "core" / "repair_note.py").exists()


# --------------------------------------------------- policy → self-repair

def test_policy_rejection_feeds_self_repair(tmp_path):
    repo = _init_repo(tmp_path)
    ledger = ReviewLedger(tmp_path / "subs.json")
    coding = FakeProvider([
        _text(PROTECTED_DIFF),   # attempt 1: violates §82
        lambda prompt, system: _text(CLEAN_DIFF) if "policy guard" in prompt
        else pytest.fail("feedback was not threaded into the regeneration prompt"),
    ])

    result = run_auto_repair(_task(), config={},
                             services={**_services([]), "coding_provider": coding},
                             max_attempts=3, repo_path=repo, ledger=ledger,
                             worktree_parent=tmp_path)

    assert result["outcome"] == "ADVANCED_TO_QUANT_VALIDATION"
    assert result["attempts_used"] == 2
    assert result["attempts"][0]["status"] == "PIPELINE_REJECTED"
    assert result["attempts"][0]["submission_id"] is None  # never entered the ledger
    assert any("PROTECTED_FILE" in f for f in result["feedback_history"])
    # only the clean attempt opened a submission
    assert len(ledger.list_submissions()) == 1
    assert worktree_status(repo, "EXP-TEST0001-R1")["exists"] is False


# ------------------------------------------------ review failure → repair

def test_review_failure_learns_and_next_attempt_passes(tmp_path):
    repo = _init_repo(tmp_path)
    ledger = ReviewLedger(tmp_path / "subs.json")
    coding = FakeProvider([
        _text(UNFINISHED_DIFF),  # attempt 1: reviewer rejects the stub
        lambda prompt, system: _text(FIXED_DIFF) if "CODE_REVIEW FAILED" in prompt
        else pytest.fail("reviewer findings were not fed back to the coding agent"),
    ])
    reviewer = FakeProvider([
        lambda prompt, system: _review("FAILED", ["stub does not implement the note"])
        if "v1 unfinished" in prompt else _review("PASSED"),
        lambda prompt, system: _review("FAILED", ["stub does not implement the note"])
        if "v1 unfinished" in prompt else _review("PASSED"),
    ])

    result = run_auto_repair(_task(), config={},
                             services={**_services([], security_script=[_review("PASSED"), _review("PASSED")]),
                                       "coding_provider": coding,
                                       "code_review_provider": reviewer},
                             max_attempts=3, repo_path=repo, ledger=ledger,
                             worktree_parent=tmp_path)

    assert result["outcome"] == "ADVANCED_TO_QUANT_VALIDATION"
    assert result["attempts_used"] == 2
    # the failed attempt is recorded honestly: REJECTED with a FAILED verdict
    rejected = [s for s in ledger.list_submissions() if s.status == "REJECTED"]
    assert len(rejected) == 1
    assert rejected[0].reviews[0]["verdict"] == "FAILED"
    assert rejected[0].reviews[0]["actor"] == "code_review_agent"
    assert result["attempts"][0]["worktree_removed"] is True
    assert worktree_status(repo, "EXP-TEST0001-R1")["exists"] is False


# ------------------------------------------- deterministic TESTS → repair

def test_tests_stage_catches_syntax_error_and_repairs(tmp_path):
    repo = _init_repo(tmp_path)
    ledger = ReviewLedger(tmp_path / "subs.json")

    result = run_auto_repair(_task(), config={},
                             services=_services(
                                 [_text(SYNTAX_ERROR_DIFF), _text(CLEAN_DIFF)],
                                 review_script=[_review("PASSED"), _review("PASSED")],
                                 security_script=[_review("PASSED"), _review("PASSED")],
                             ),
                             max_attempts=3, repo_path=repo, ledger=ledger,
                             worktree_parent=tmp_path)

    assert result["outcome"] == "ADVANCED_TO_QUANT_VALIDATION"
    assert result["attempts_used"] == 2
    first = ledger.list_submissions(experiment_id="EXP-TEST0001-R1")[0]
    tests_verdict = next(r for r in first.reviews if r["stage"] == "TESTS")
    assert tests_verdict["verdict"] == "FAILED"
    assert tests_verdict["actor"] == "test_agent"
    assert any("py_compile failed" in f for f in result["feedback_history"])


# -------------------------------------------------------- exhaustion/honesty

def test_exhausted_reports_honestly(tmp_path):
    repo = _init_repo(tmp_path)
    ledger = ReviewLedger(tmp_path / "subs.json")
    coding = FakeProvider([_text(PROTECTED_DIFF), _text(PROTECTED_DIFF)])

    result = run_auto_repair(_task(), config={},
                             services=_services([]) | {"coding_provider": coding},
                             max_attempts=2, repo_path=repo, ledger=ledger,
                             worktree_parent=tmp_path)

    assert result["outcome"] == "EXHAUSTED"
    assert result["attempts_used"] == 2
    assert len(result["feedback_history"]) >= 2
    assert result["final_submission"] is None


def test_no_provider_fails_closed(tmp_path):
    repo = _init_repo(tmp_path)
    ledger = ReviewLedger(tmp_path / "subs.json")

    result = run_auto_repair(_task(), config={}, services=None,
                             repo_path=repo, ledger=ledger, worktree_parent=tmp_path)

    assert result["outcome"] == "NO_PROVIDER"
    assert result["attempts_used"] == 0
    assert ledger.list_submissions() == []


def test_reviewer_unavailable_records_not_performed(tmp_path):
    repo = _init_repo(tmp_path)
    ledger = ReviewLedger(tmp_path / "subs.json")
    services = _services([_text(CLEAN_DIFF)])
    del services["code_review_provider"]  # reviewers unconfigured

    result = run_auto_repair(_task(), config={}, services=services,
                             repo_path=repo, ledger=ledger, worktree_parent=tmp_path)

    assert result["outcome"] == "REVIEW_UNAVAILABLE"
    attempt = result["attempts"][0]
    assert attempt["stages"] == {"CODE_REVIEW": "NOT_PERFORMED"}
    submission = ledger.get(attempt["submission_id"])
    assert submission.reviews[0]["verdict"] == "NOT_PERFORMED"  # honest, never faked
    assert attempt["worktree_removed"] is True


def test_task_requires_problem_and_experiment(tmp_path):
    with pytest.raises(AutoRepairError):
        run_auto_repair(RepairTask(experiment_id="", problem="x"), config={},
                        services=_services([]), repo_path=_init_repo(tmp_path / "r1"),
                        worktree_parent=tmp_path)
    with pytest.raises(AutoRepairError):
        run_auto_repair(RepairTask(experiment_id="E", problem=" "), config={},
                        services=_services([]), repo_path=_init_repo(tmp_path / "r2"),
                        worktree_parent=tmp_path)


# ------------------------------------------------------ experiment bridging

def test_build_task_from_experiment(tmp_path):
    from core.research_factory.experiments import ExperimentRegistry
    from core.research_factory.hypothesis import HypothesisRecord, HypothesisStore

    store = HypothesisStore(tmp_path / "hyp.json")
    store.append(HypothesisRecord(
        hypothesis_id="hyp_1", problem="Signals lack the new note",
        observation="notes missing", hypothesis="add repair note",
        expected_mechanism="documentation clarity", baseline="no note",
        challenger="note module", universe="SP500", horizon="daily",
        dataset="internal", metrics=["clarity"], promotion_criteria=["all pass"],
        failure_criteria=["any fail"], risks=["none"],
    ))
    registry = ExperimentRegistry(tmp_path / "exp.json", hypothesis_store=store)
    experiment = registry.propose(hypothesis_id="hyp_1", strategy_id="strat_a",
                                  version="1", description="Implement the note module")

    task = build_task_from_experiment(experiment.experiment_id,
                                      registry=registry, hypothesis_store=store)
    assert task.experiment_id == experiment.experiment_id
    assert "add repair note" in task.problem
    assert task.context["hypothesis"]["hypothesis_id"] == "hyp_1"
    assert task.context["experiment"]["strategy_id"] == "strat_a"

    with pytest.raises(AutoRepairError):
        build_task_from_experiment("exp_missing", registry=registry, hypothesis_store=store)


# ------------------------------------------------------------- config gate

def test_auto_repair_config_defaults_off_and_clamps():
    assert auto_repair_config(None)["enabled"] is False
    assert auto_repair_config({})["max_attempts"] == 3
    assert auto_repair_config({"development_factory": {"auto_repair": {
        "enabled": True, "max_attempts": 99}}})["max_attempts"] == 5


# ------------------------------------------- sandbox tolerates LLM miscounts

def test_none_config_falls_back_to_shared_runtime_config(monkeypatch):
    """The watchdog daemon thread calls the chain WITHOUT config — provider
    resolution must fall back to the shared runtime config, not report
    'no AI provider' (the glm global assignment is read from there)."""
    from core.development_factory import auto_repair as ar

    seen = {}

    class FakeShared:
        config = {"ai": {"providers": {"ollama": {"enabled": True, "type": "ollama"}}}}

    import api.state_service as state_service

    monkeypatch.setattr(state_service, "get_shared_config", lambda: FakeShared())
    original_build = ar.build_provider if hasattr(ar, "build_provider") else None

    import api.ai_config_service as ai_config_service

    def fake_build(cfg, name, **kwargs):
        seen["provider"] = name
        return object()  # any non-None provider proves resolution worked

    monkeypatch.setattr(ai_config_service, "build_provider", fake_build)
    roles = ar._role_services(None, None)
    assert seen.get("provider") == "ollama"
    assert roles["coding"][0] is not None
    assert roles["code_review"][0] is not None

def test_normalize_diff_rewrites_new_file_modification_form(tmp_path):
    from core.development_factory.auto_repair import normalize_diff_for_apply

    repo = _init_repo(tmp_path)
    llm_shaped = (  # model formats a NEW file as a modification, with '?' artifacts
        "--- a/core/new_helper.py\n"
        "+++ b/core/new_helper.py\n"
        "@@ -1,3 +1,4 @@\n"
        " import json\n"
        "+VALUE = 42\n"
        "?unexpected artifact\n"
        "-OLD = 1\n"
    )
    normalized = normalize_diff_for_apply(llm_shaped, repo)
    assert "--- /dev/null" in normalized
    assert "+++ b/core/new_helper.py" in normalized
    assert "@@ -0,0 +1,2 @@" in normalized      # import json + VALUE = 42
    assert "+import json" in normalized         # context became content
    assert "+VALUE = 42" in normalized
    assert "OLD" not in normalized              # removal dropped for a new file
    assert "?" not in normalized
    from core.development_factory.sandbox import apply_unified_diff
    assert apply_unified_diff(repo, normalized)["applied"] is True
    content = (repo / "core" / "new_helper.py").read_text(encoding="utf-8")
    assert "import json" in content and "VALUE = 42" in content


def test_normalize_diff_leaves_existing_file_hunks_semantically_intact(tmp_path):
    from core.development_factory.auto_repair import normalize_diff_for_apply
    from core.development_factory.sandbox import apply_unified_diff

    repo = _init_repo(tmp_path)
    modification = (
        "--- a/core/x.py\n"
        "+++ b/core/x.py\n"
        "@@ -1,1 +1,2 @@\n"
        " x = 1\n"
        "+y = 2\n"
    )
    normalized = normalize_diff_for_apply(modification, repo)
    assert normalized.startswith("diff --git a/core/x.py b/core/x.py\n")
    assert " x = 1\n+y = 2" in normalized          # hunk body byte-identical
    assert apply_unified_diff(repo, normalized)["applied"] is True


def test_normalize_diff_handles_edit_plus_new_file_together(tmp_path):
    """The live failure: an existing-file edit followed by a new-file section."""
    from core.development_factory.auto_repair import normalize_diff_for_apply
    from core.development_factory.sandbox import apply_unified_diff

    repo = _init_repo(tmp_path)
    combined = (
        "--- a/core/x.py\n"
        "+++ b/core/x.py\n"
        "@@ -1,1 +1,2 @@\n"
        " x = 1\n"
        "+y = 2\n"
        "--- a/core/new_helper.py\n"      # model formats the NEW file as a modification
        "+++ b/core/new_helper.py\n"
        "@@ -1,2 +1,2 @@\n"
        " import json\n"
        "+VALUE = 42\n"
        "-OLD = 1\n"
    )
    normalized = normalize_diff_for_apply(combined, repo)
    assert apply_unified_diff(repo, normalized)["applied"] is True
    assert (repo / "core" / "new_helper.py").read_text(encoding="utf-8") == (
        "import json\nVALUE = 42\n"
    )
    assert "y = 2" in (repo / "core" / "x.py").read_text(encoding="utf-8")


def test_normalize_diff_converts_absolute_repo_path_to_git_relative(tmp_path):
    from core.development_factory.auto_repair import normalize_diff_for_apply
    from core.development_factory.sandbox import apply_unified_diff

    repo = _init_repo(tmp_path)
    absolute = (repo / "core" / "x.py").resolve().as_posix()
    generated = (
        f"--- a/{absolute}\n"
        f"+++ b/{absolute}\n"
        "@@ -1,1 +1,2 @@\n"
        " x = 1\n"
        "+fixed = True\n"
    )

    normalized = normalize_diff_for_apply(generated, repo)

    assert "C:/" not in normalized
    assert "+++ b/core/x.py" in normalized
    assert apply_unified_diff(repo, normalized)["applied"] is True


def test_role_resolution_preserves_owner_selected_cloud_models(monkeypatch):
    from core.development_factory import auto_repair as ar

    monkeypatch.setattr("api.ai_config_service.build_provider", lambda _cfg, _name: object())
    cfg = {
        "ai": {
            "providers": {"ollama": {"enabled": True}},
            "assignment": {
                "coding_agent": "glm-5.2:cloud",
                "code_reviewer": "gpt-oss:120b:cloud",
                "security_reviewer": "gemma4:26b",
            },
        }
    }

    roles = ar._role_services(cfg, None)

    assert roles["coding"][1] == "glm-5.2:cloud"
    assert roles["code_review"][1] == "gpt-oss:120b:cloud"
    assert roles["security_review"][1] == "gemma4:26b"


def test_sandbox_recount_applies_miscounted_llm_hunk(tmp_path):
    from core.development_factory.sandbox import apply_unified_diff

    repo = _init_repo(tmp_path)
    miscounted = (  # header claims 4 added lines, body has 2 (classic LLM error)
        "--- /dev/null\n"
        "+++ b/core/miscounted.py\n"
        "@@ -0,0 +1,4 @@\n"
        "+NOTE = \"recount\"\n"
        "+VALUE = 1\n"
    )
    result = apply_unified_diff(repo, miscounted)
    assert result["applied"] is True
    assert (repo / "core" / "miscounted.py").read_text(encoding="utf-8").startswith("NOTE")


# ---------------------------------------------------------- API endpoint

def test_auto_repair_endpoint_gate_and_honest_no_provider(client, monkeypatch):
    monkeypatch.setattr("api.state_service.get_shared_config", lambda: {})
    response = client.post("/api/development/auto-repair", json={"problem": "fix X"})
    assert response.status_code == 403  # owner gate: disabled by default

    monkeypatch.setattr("api.state_service.get_shared_config", lambda: {
        "development_factory": {"auto_repair": {"enabled": True}}})
    response = client.post("/api/development/auto-repair", json={})
    assert response.status_code == 422  # experiment_id or problem required

    response = client.post("/api/development/auto-repair", json={"problem": "fix X"})
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "NO_PROVIDER"  # honest — no faked submissions
    assert body["attempts_used"] == 0
