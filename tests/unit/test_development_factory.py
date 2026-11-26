"""Phase 15 unit tests — §82/§83/§84 diff guard and §81 review ledger."""
import pytest

from core.development_factory.policy import DiffRejected, enforce_diff, review_diff
from core.development_factory.review import ReviewError, ReviewLedger


def _diff(path, removed=(), added=()):
    lines = [f"--- a/{path}", f"+++ b/{path}", "@@ -1,1 +1,1 @@"]
    lines += [f"-{line}" for line in removed]
    lines += [f"+{line}" for line in added]
    return "\n".join(lines)


def test_protected_files_are_rejected():
    for path in ("docs/V9_RISK_CONSTITUTION.md", "core/autonomy.py",
                 "core/strategy_registry/policy.py"):
        findings = review_diff(_diff(path, added=["x = 1"]))
        assert any(f.rule == "PROTECTED_FILE" for f in findings), path


def test_golden_test_modification_is_rejected():
    findings = review_diff(_diff("tests/golden/test_phase9_golden.py", added=["assert True"]))
    assert any(f.rule == "GOLDEN_TEST_MODIFIED" for f in findings)
    with pytest.raises(DiffRejected):
        enforce_diff(_diff("tests/golden/test_phase13_golden.py"))


def test_skip_marker_additions_are_goal_hacking():
    findings = review_diff(_diff("tests/unit/test_anything.py",
                                 added=["pytestmark = pytest.mark.skip(reason='x')"]))
    assert any(f.rule == "TEST_WEAKENED" for f in findings)
    findings = review_diff(_diff("tests/unit/test_anything.py", added=["pytest.skip()"]))
    assert any(f.rule == "TEST_WEAKENED" for f in findings)


def test_cost_parameter_removal_is_flagged():
    findings = review_diff(_diff("core/backtest/engine.py", removed=["slippage_bps = 5.0"]))
    assert any(f.rule == "EVALUATION_WEAKENED" for f in findings)
    clean = review_diff(_diff("core/backtest/engine.py", removed=["window = 20"],
                              added=["window = 30"]))
    assert clean == []


def test_clean_diff_passes():
    assert review_diff(_diff("core/some_module.py", added=["def better(): ..."])) == []


@pytest.fixture()
def ledger(tmp_path):
    return ReviewLedger(tmp_path / "subs.json")


def _open(ledger, author="coding_agent"):
    return ledger.open_submission(
        experiment_id="EXP-0001", author=author, branch="ai/experiment/EXP-0001",
        worktree="/tmp/wt-EXP-0001", diff_sha256="a" * 64,
    )


def test_implementer_cannot_review_own_submission(ledger):
    submission = _open(ledger, author="coding_agent")
    with pytest.raises(ReviewError):
        ledger.record_review(submission.submission_id, stage="CODE_REVIEW",
                             actor="coding_agent", verdict="PASSED")


def test_wrong_actor_cannot_record_stage(ledger):
    submission = _open(ledger)
    with pytest.raises(ReviewError):
        ledger.record_review(submission.submission_id, stage="CODE_REVIEW",
                             actor="somebody_else", verdict="PASSED")


def test_full_chain_to_shadow_ready(ledger):
    submission = _open(ledger)
    stages = [
        ("CODE_REVIEW", "code_review_agent"),
        ("SECURITY_REVIEW", "security_review_agent"),
        ("TESTS", "test_agent"),
        ("QUANT_VALIDATION", "quant_validator"),
        ("E2E", "e2e_agent"),
    ]
    current = submission
    for stage, actor in stages:
        current = ledger.record_review(
            current.submission_id, stage=stage, actor=actor, verdict="PASSED",
            evidence={"deterministic": True},
        )
    assert current.status == "SHADOW_READY"
    assert len({review["actor"] for review in current.reviews}) == 5


def test_failed_stage_rejects_submission(ledger):
    submission = _open(ledger)
    updated = ledger.record_review(submission.submission_id, stage="CODE_REVIEW",
                                   actor="code_review_agent", verdict="FAILED",
                                   evidence={"finding": "sql injection"})
    assert updated.status == "REJECTED"
    with pytest.raises(ReviewError):  # closed for further reviews
        ledger.record_review(submission.submission_id, stage="SECURITY_REVIEW",
                             actor="security_review_agent", verdict="PASSED")


def test_reviews_are_append_only(ledger):
    submission = _open(ledger)
    ledger.record_review(submission.submission_id, stage="CODE_REVIEW",
                         actor="code_review_agent", verdict="PASSED")
    with pytest.raises(ReviewError):
        ledger.record_review(submission.submission_id, stage="CODE_REVIEW",
                             actor="code_review_agent", verdict="PASSED")
