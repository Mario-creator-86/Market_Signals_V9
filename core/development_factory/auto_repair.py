"""Phase 15+ §81/§112 — the LLM coding agent with a bounded self-repair loop.

The missing §112 middle: for a research experiment (or a direct problem
statement) the Coding Agent role GENERATES a unified diff, the diff passes the
§82/§83/§84 policy guard and is applied to an isolated worktree (§80), then
independent LLM reviewers (code_reviewer / security_reviewer §37 roles) and a
deterministic sandbox test stage record verdicts in the append-only ledger
(§81). When any gate fails, the failure feedback is fed back to the coding
agent for the next attempt — the system repairs itself, bounded by
`max_attempts`. The implementer never records a review stage; a reviewer that
cannot run is recorded honestly as NOT_PERFORMED, never faked as PASSED.

Nothing here merges, deploys, promotes or touches production files: the
journey still ends at the Phase 15 boundary (QUANT_VALIDATION/E2E remain for
Phase 16/17 actors).
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.ai.assignment import ModelAssignment
from core.ai.fallback import all_unavailable, generate_with_fallback
from core.ai.provider import AIProvider, AIResponse, is_model_bound_provider
from core.ai.router import ModelRouter
from core.development_factory.pipeline import (
    DevelopmentPipelineError, diff_sha256, submit_change,
)
from core.development_factory.policy import PROTECTED_PATHS, review_diff
from core.development_factory.review import ReviewLedger, STAGE_ACTORS
from core.development_factory.sandbox import remove_worktree

logger = logging.getLogger(__name__)

AUTHOR_ACTOR = "coding_agent"
DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS_LIMIT = 5
MAX_DIFF_CHARS = 200_000
MAX_CONTEXT_CHARS = 6_000
MAX_CONTEXT_FILES_CHARS = 24_000  # total budget for exact source excerpts

# outcome values
OUTCOME_ADVANCED = "ADVANCED_TO_QUANT_VALIDATION"
OUTCOME_EXHAUSTED = "EXHAUSTED"
OUTCOME_NO_PROVIDER = "NO_PROVIDER"
OUTCOME_GENERATION_UNAVAILABLE = "GENERATION_UNAVAILABLE"
OUTCOME_REVIEW_UNAVAILABLE = "REVIEW_UNAVAILABLE"
OUTCOME_NO_DEFECT = "NO_CODE_DEFECT"


class AutoRepairError(ValueError):
    """Bounded, caller-facing error (422-class)."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _attempt_event(record: Dict[str, Any], stage: str, status: str,
                   detail: str = "", evidence: Optional[Dict[str, Any]] = None) -> None:
    """Append one durable, UI-safe event to an attempt's execution trail."""
    event: Dict[str, Any] = {
        "at": _utc_now(),
        "stage": str(stage or "unknown"),
        "status": str(status or "INFO"),
        "detail": str(detail or "")[:1000],
    }
    if evidence:
        event["evidence"] = dict(evidence)
    record.setdefault("events", []).append(event)


def _role_assignment_summary(
    roles: Dict[str, Tuple[Optional[AIProvider], Optional[str]]],
) -> Dict[str, Dict[str, str]]:
    """Expose provider/model identity without serializing provider internals."""
    summary: Dict[str, Dict[str, str]] = {}
    for role, (provider, model) in roles.items():
        provider_name = ""
        if provider is not None:
            provider_name = str(getattr(provider, "name", "") or provider.__class__.__name__)
        summary[role] = {"provider": provider_name, "model": str(model or "")}
    return summary


@dataclass(frozen=True)
class RepairTask:
    """What the coding agent must implement (§76 hypothesis context or direct problem)."""
    experiment_id: str
    problem: str
    target_hint: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    # repo path -> exact current source excerpt; diff context lines must match
    # these byte-for-byte, which is what lets the agent modify real files.
    context_files: Dict[str, str] = field(default_factory=dict)


def read_context_files(
    repo_path: Path, paths: List[str], *,
    max_total: int = MAX_CONTEXT_FILES_CHARS,
) -> Dict[str, str]:
    """Read bounded exact excerpts of `paths` under `repo_path` (missing files skipped)."""
    root = Path(repo_path).resolve()
    excerpts: Dict[str, str] = {}
    remaining = max_total
    for rel in paths:
        if remaining <= 0:
            break
        target = (root / rel).resolve()
        if root != target and root not in target.parents:
            continue  # never read outside the repository
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        excerpts[rel] = text[:remaining]
        remaining -= len(excerpts[rel])
    return excerpts


def auto_repair_config(config: Any) -> Dict[str, Any]:
    """Read `development_factory.auto_repair` from config (tolerant, §owner-gated)."""
    root = getattr(config, "config", config) if config is not None else {}
    section = ((root or {}).get("development_factory") or {}).get("auto_repair") if isinstance(root, dict) else None
    if not isinstance(section, dict):
        section = {}
    try:
        attempts = int(section.get("max_attempts", DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS)
    except (TypeError, ValueError):
        attempts = DEFAULT_MAX_ATTEMPTS
    return {
        "enabled": bool(section.get("enabled", False)),
        "max_attempts": max(1, min(MAX_ATTEMPTS_LIMIT, attempts)),
    }


# --------------------------------------------------------------- provider seam

def _runtime_ai_config(config: Any) -> Dict[str, Any]:
    """The config dict for provider resolution; None falls back to the shared
    runtime config (the watchdog daemon calls without one)."""
    if isinstance(config, dict):
        return config
    if config is not None:
        merged = getattr(config, "config", None)
        if isinstance(merged, dict):
            return merged
    try:  # lazy import avoids a core->api cycle
        from api.state_service import get_shared_config

        shared = get_shared_config()
        merged = getattr(shared, "config", shared)
        return merged if isinstance(merged, dict) else {}
    except Exception as exc:
        logger.warning("auto_repair: shared runtime config unavailable: %s", exc)
        return {}


def _resolve_role(config: Any, role: str, tier: str) -> Tuple[Optional[AIProvider], Optional[str]]:
    """Resolve (provider, model) for a §37 role. None when nothing is configured."""
    cfg = _runtime_ai_config(config)
    model = None
    try:
        assignment = ModelAssignment.from_config(cfg)
        router = ModelRouter.from_config(cfg, assignment)
        model = router.select_model(role, tier_hint=tier)
    except Exception as exc:
        logger.warning("auto_repair: model resolution failed for role %s: %s", role, exc)
        model = None
    # Lazy import to avoid a core->api cycle at import time (same seam as §44 critic).
    from api.ai_config_service import build_provider
    providers = (cfg.get("ai") or {}).get("providers") or {}

    def _build(name: str) -> Optional[AIProvider]:
        try:
            return build_provider(cfg, name)
        except Exception as exc:
            logger.warning("auto_repair: provider %s failed to build: %s", name, exc)
            return None

    # A model-bound provider (the Claude subscription) serves only its own
    # models: it wins when the role's model is one of them and is never the
    # generic choice for another provider's model.
    bound = [name for name, entry in providers.items() if is_model_bound_provider(entry)]
    if model:
        for name in bound:
            prov = _build(name)
            serves = getattr(prov, "serves_model", None)
            if prov is not None and callable(serves) and serves(model):
                return prov, model
    for name in providers:
        if name in bound:
            continue
        prov = _build(name)
        if prov is not None:
            return prov, model
    return None, model


def _role_services(
    config: Any, services: Optional[Dict[str, Any]],
) -> Dict[str, Tuple[Optional[AIProvider], Optional[str]]]:
    """Merge injected services (tests) over config-resolved (provider, model) per role."""
    svc = services or {}
    resolved: Dict[str, Tuple[Optional[AIProvider], Optional[str]]] = {}
    for key, role, tier in (
        ("coding", "coding_agent", "analysis"),
        ("code_review", "code_reviewer", "critic"),
        ("security_review", "security_reviewer", "critic"),
    ):
        provider = svc.get(f"{key}_provider")
        model = svc.get(f"{key}_model")
        if provider is None:
            provider, model = _resolve_role(config, role, tier)
        resolved[key] = (provider, model)
    return resolved


# ------------------------------------------------------------ prompts + parsing

_CODING_SYSTEM = (
    "You are the autonomous Coding Agent (spec §81/§112). You implement ONE "
    "bounded change as a unified diff. You never review your own change. "
    "Answer with the unified diff ONLY — no prose, no markdown fences."
)

_CODING_PROMPT = """Implement the following bounded code change.

Problem:
{problem}

Task context:
{context}
{target_hint}{context_files_section}
Hard constraints:
- Output ONLY a unified diff (--- a/<path> / +++ b/<path> hunks, git format). No prose.
- Produce the SMALLEST possible hunks: change only the lines that must change and copy
  the surrounding context lines EXACTLY (byte-for-byte, same indentation). Never
  reformat or include unchanged lines inside a hunk; never rewrite a whole function.
  Prefer adding a new helper file plus a tiny anchored edit over large replacements.
- Keep the TOTAL diff under ~80 lines: compact code, docstrings of ONE line only,
  no comments unless essential. Your output is token-limited — a longer diff WILL be
  truncated mid-line and rejected.
- The diff may contain only ' ' (context), '+', '-', '@@', '--- ' and '+++ ' lines —
  never '?' or '\\' lines.
- Your diff context lines MUST match the provided source excerpts byte-for-byte
  (when excerpts are given); keep hunks small and anchored on exact context.
- NEVER touch protected paths: {protected}
- NEVER modify anything under tests/golden/, add pytest skip/xfail markers, or remove
  slippage/fees/cost parameters from backtest code (the policy guard rejects such diffs).
- Keep the change minimal and focused. New helper files are allowed only inside an
  existing package and must be paired with an anchored change in the incident scope;
  never invent a top-level module as a substitute for editing the failing path.
- If you determine the problem has NO code-level cause, do NOT invent a diff:
  answer exactly 'NO_CODE_DEFECT: <one-line reason>' instead.
{feedback}"""

_CONTEXT_FILES_HEADER = (
    "\nExact current source excerpts (anchor your diff context lines to these "
    "byte-for-byte):\n"
)

_FEEDBACK_HEADER = "\nPrevious attempts FAILED. Repair the change and try again:\n"

_REVIEW_PROMPTS = {
    "CODE_REVIEW": (
        "You are the independent Code Review Agent (spec §81). The implementer is a "
        "DIFFERENT agent; judge only the evidence below — never trust the author's claims.\n"
        "Problem being solved:\n{problem}\n\nUnified diff to review:\n{diff}\n\n"
        "Check: correctness, minimal scope, obvious bugs, dead code, misleading names.\n"
        'Return strict JSON: {{"verdict": "PASSED"|"FAILED", "findings": ["..."], '
        '"rationale": "..."}}.'
    ),
    "SECURITY_REVIEW": (
        "You are the independent Security Review Agent (spec §81). The implementer is a "
        "DIFFERENT agent; judge only the evidence below.\n"
        "Problem being solved:\n{problem}\n\nUnified diff to review:\n{diff}\n\n"
        "Check: secrets/credentials, command/SQL injection, unsafe deserialization, "
        "path escapes, overly broad permissions, network calls to unknown hosts.\n"
        'Return strict JSON: {{"verdict": "PASSED"|"FAILED", "findings": ["..."], '
        '"rationale": "..."}}.'
    ),
}


def _extract_json_object(raw_text: str) -> Optional[dict]:
    text = str(raw_text or "").strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def extract_diff(raw_text: str) -> str:
    """Pull the unified diff out of an LLM answer (fences/prose tolerant, fail-closed)."""
    text = str(raw_text or "").replace("\r\n", "\n").strip()
    lines = text.split("\n")
    start = next(
        (i for i, line in enumerate(lines)
         if line.startswith("--- ") or line.startswith("diff --git")),
        None,
    )
    if start is None:
        return ""
    cleaned: List[str] = []
    for line in lines[start:]:
        if line.strip() == "```":
            break
        cleaned.append(line)
    body = "\n".join(cleaned).strip("\n")
    if not body or "+++ b/" not in body or len(body) > MAX_DIFF_CHARS:
        return ""
    return body + "\n"  # git apply requires the hunk to end with a newline


# ----------------------------------------------------------------- LLM stages

def generate_repair_diff(
    task: RepairTask, feedback: List[str], *, roles: Dict[str, Tuple[Optional[AIProvider], Optional[str]]],
) -> str:
    """One coding-agent generation. Raises AutoRepairError when no diff can be produced."""
    provider, model = roles["coding"]
    if provider is None:
        raise AutoRepairError("coding agent provider unavailable (no AI provider enabled)")
    try:
        context = json.dumps(task.context, ensure_ascii=False, default=str)
    except Exception:
        context = str(task.context)
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "…[truncated]"
    feedback_section = ""
    if feedback:
        feedback_section = _FEEDBACK_HEADER + "\n".join(f"- {f}" for f in feedback[-8:])
    context_files_section = ""
    if task.context_files:
        parts: List[str] = []
        budget = MAX_CONTEXT_FILES_CHARS
        for path, content in task.context_files.items():
            if budget <= 0:
                break
            excerpt = str(content)[:budget]
            parts.append(f"----- {path} -----\n{excerpt}")
            budget -= len(excerpt)
        context_files_section = _CONTEXT_FILES_HEADER + "\n\n".join(parts)
    prompt = _CODING_PROMPT.format(
        problem=str(task.problem or "").strip() or "(none provided)",
        context=context,
        target_hint=f"\nTarget hint: {task.target_hint}\n" if task.target_hint else "",
        context_files_section=context_files_section,
        protected=", ".join(PROTECTED_PATHS) + ", tests/golden/**",
        feedback=feedback_section,
    )
    # GOLD-013 resilience for the coding stage. All §37 roles may resolve to the
    # same provider object, so provider-identity dedup is useless here — what
    # differs is the MODEL (kimi coding vs glm reviewers): a different remote
    # backend is real redundancy. Diff generation on cloud models with real
    # source excerpts routinely exceeds the 120s default, hence the override.
    candidates: List[Tuple[Optional[AIProvider], Optional[str]]] = [(provider, model)]
    for key in ("code_review", "security_review"):
        p, m = roles.get(key) or (None, None)
        if p is not None and m and m != model:
            candidates.append((p, m))
    resp = None
    failures: List[str] = []
    for prov, mdl in candidates:
        try:
            attempt_resp = generate_with_fallback(
                prov, prompt, model=mdl, system=_CODING_SYSTEM, timeout_override=300,
            )
        except Exception as exc:
            failures.append(f"{mdl or 'default'}: {exc.__class__.__name__}: {exc}"[:240])
            continue
        if not all_unavailable(attempt_resp):
            resp = attempt_resp
            break
        failures.append(f"{mdl or 'default'}: all providers unavailable")
    if resp is None:
        detail = "; ".join(failures)[:500] or "no provider candidates resolved"
        raise AutoRepairError(
            "coding agent generation unavailable (all providers failed: " + detail + ")")
    raw = getattr(resp, "text", "") or ""
    if "NO_CODE_DEFECT" in raw:
        reason = raw.strip().split("\n", 1)[0][:300]
        raise AutoRepairError(f"coding agent reported no code defect: {reason}")
    diff = extract_diff(raw)
    if not diff:
        raise AutoRepairError("coding agent output was not a parseable unified diff")
    return diff


def run_llm_review(
    stage: str, task: RepairTask, diff: str, *,
    roles: Dict[str, Tuple[Optional[AIProvider], Optional[str]]],
) -> Tuple[str, Dict[str, Any]]:
    """Independent LLM review of one stage. Returns (verdict, evidence); fail-closed."""
    key = "code_review" if stage == "CODE_REVIEW" else "security_review"
    provider, model = roles[key]
    if provider is None:
        return "NOT_PERFORMED", {"rationale": f"{stage} reviewer provider unavailable"}
    prompt = _REVIEW_PROMPTS[stage].format(
        problem=str(task.problem or "")[:1500], diff=diff[:MAX_DIFF_CHARS],
    )
    system = f"You are the {STAGE_ACTORS[stage]} (spec §81). Independent, strict, honest."
    try:
        resp = generate_with_fallback(provider, prompt, model=model, system=system)
    except Exception as exc:
        return "NOT_PERFORMED", {
            "rationale": f"{stage} reviewer call raised: {exc.__class__.__name__}: {exc}"[:500],
        }
    if all_unavailable(resp):
        return "NOT_PERFORMED", {"rationale": f"{stage} reviewer call failed (providers unavailable)"}
    parsed = _extract_json_object(getattr(resp, "text", "") or "")
    if parsed is None:
        # A review that ran but produced no parseable verdict must never pass (§81 honesty).
        return "FAILED", {"rationale": f"{stage} reviewer output unparseable",
                          "findings": ["reviewer did not return strict JSON"]}
    verdict = str(parsed.get("verdict") or "").upper().strip()
    if verdict not in {"PASSED", "FAILED"}:
        return "FAILED", {"rationale": f"{stage} reviewer verdict invalid: {verdict!r}",
                          "findings": ["verdict must be PASSED or FAILED"]}
    findings = [str(f) for f in (parsed.get("findings") or [])][:20]
    return verdict, {
        "findings": findings,
        "rationale": str(parsed.get("rationale") or "")[:1000],
        "reviewer_role": STAGE_ACTORS[stage],
        "model": model or "",
    }


# ------------------------------------------------------- deterministic TESTS

def run_sandbox_tests(worktree_path: Path, diff_text: str) -> Tuple[str, Dict[str, Any]]:
    """§81 TESTS stage (deterministic, no LLM): py-compile every .py file the diff touches."""
    from core.development_factory.policy import _parse_files

    py_files = []
    for section in _parse_files(diff_text):
        path = section["path"]
        if path.endswith(".py"):
            py_files.append(path)
    worktree = Path(worktree_path)
    failures: List[str] = []
    for rel in py_files:
        target = worktree / rel
        if not target.exists():
            failures.append(f"{rel}: file missing in worktree")
            continue
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "py_compile", str(target)],
                capture_output=True, text=True, timeout=60, cwd=str(worktree),
            )
        except Exception as exc:  # timeout / spawn failure — honest, not fatal-fake
            failures.append(f"{rel}: compile check could not run ({exc})")
            continue
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            failures.append(f"{rel}: py_compile failed — {detail[-1][:200] if detail else 'no output'}")
    evidence = {
        "command": "python -m py_compile",
        "files_checked": py_files,
        "failures": failures,
        "note": "deterministic sandbox check; full test runner is the Phase 17 actor",
    }
    return ("FAILED" if failures else "PASSED"), evidence


def normalize_diff_for_apply(diff_text: str, repo_path: Path) -> str:
    """Canonicalize LLM diff quirks so intent survives `git apply`.

    For every target file that does NOT exist in the repo (a new file), rewrite
    the section into the canonical new-file form: `--- /dev/null`, a single
    `@@ -0,0 +1,N @@` hunk built from the added lines (context lines become
    additions — the model wrote them as intended content; removal lines are
    dropped — there is nothing to remove). Sections touching existing files
    pass through verbatim (their `--- a/` header reconstructed if missing);
    '`?`' artifact lines are dropped.
    """
    root = Path(repo_path).resolve()
    lines = str(diff_text or "").replace("\r\n", "\n").split("\n")

    sections: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {"minus": None, "plus": None, "body": []}
    pending_minus: Optional[str] = None

    def flush() -> None:
        nonlocal current, pending_minus
        if current.get("plus"):
            sections.append(current)
        current = {"minus": None, "plus": None, "body": []}

    for line in lines:
        if line.startswith("--- ") and not line.startswith("----"):
            flush()  # a `---` header closes the previous section (git ordering)
            pending_minus = line
        elif line.startswith("+++ "):
            plus = line[4:].strip()
            if plus.startswith("b/"):
                plus = plus[2:]
            current = {"minus": pending_minus, "plus": plus, "body": []}
            pending_minus = None
        else:
            if current.get("plus"):
                current["body"].append(line)
            # preamble or stray lines between headers are dropped
    flush()

    out: List[str] = []
    for sec in sections:
        raw_path = str(sec["plus"] or "").strip().replace("\\", "/")
        candidate = Path(raw_path)
        if candidate.is_absolute():
            try:
                path = candidate.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
        else:
            path = candidate.as_posix().lstrip("/")
        if not path or path == "/dev/null":
            continue
        if path == ".." or path.startswith("../"):
            continue
        if (root / path).exists():
            out.append(f"diff --git a/{path} b/{path}")
            out.extend([f"--- a/{path}", f"+++ b/{path}"])
            out.extend(line for line in sec["body"] if not line.startswith("?"))
            continue
        added: List[str] = []
        body = list(sec["body"])
        if body and body[-1] == "":
            body = body[:-1]  # split artifact, not a real blank content line
        for line in body:
            if line.startswith("?") or line.startswith("\\"):
                continue  # artifact / "\ No newline" lines
            if line.startswith("@@"):
                continue  # hunk headers are re-emitted below
            if line.startswith("+"):
                added.append(line)
            elif line.startswith(" "):
                added.append("+" + line[1:])   # context becomes content
            elif line.startswith("-"):
                continue                        # nothing to remove in a new file
            else:
                added.append("+" + line)        # unmarked content line (LLM quirk)
        out.append(f"diff --git a/{path} b/{path}")
        out.append("new file mode 100644")
        out.extend([f"--- /dev/null", f"+++ b/{path}", f"@@ -0,0 +1,{len(added)} @@"])
        out.extend(added)
    text = "\n".join(out).strip("\n")
    return text + "\n" if text else ""


def repair_scope_findings(task: RepairTask, diff_text: str, repo_path: Path) -> List[str]:
    """Reject syntactically valid but incident-irrelevant autonomous patches."""
    paths = [
        line[6:].strip().replace("\\", "/")
        for line in str(diff_text or "").splitlines()
        if line.startswith("+++ b/")
    ]
    findings: List[str] = []
    root = Path(repo_path).resolve()
    for path in paths:
        target = (root / path).resolve()
        if len(Path(path).parts) == 1 and path.endswith(".py") and not target.exists():
            findings.append(f"unrelated top-level module is not allowed: {path}")

    allowed_raw = (task.context or {}).get("allowed_files") or []
    if isinstance(allowed_raw, str):
        allowed_raw = [allowed_raw]
    allowed = {
        str(path).strip().replace("\\", "/")
        for path in allowed_raw
        if str(path).strip()
    }
    if allowed and not any(path in allowed for path in paths):
        findings.append(
            "diff does not touch any incident source file: " + ", ".join(sorted(allowed))
        )
    return findings


# ------------------------------------------------------------- the repair loop

def run_auto_repair(
    task: RepairTask, *,
    config: Any = None,
    services: Optional[Dict[str, Any]] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    repo_path: Optional[Path] = None,
    ledger: Optional[ReviewLedger] = None,
    worktree_parent: Optional[Path] = None,
    progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """Bounded §112 self-repair loop: generate → guard → sandbox → review → feedback.

    Returns an honest report: every attempt, every verdict, every feedback item.
    `progress(stage, detail)` is an optional live-observer callback fired at
    each chain stage (generation, policy, each review, tests, outcome).
    """

    def _emit(stage: str, detail: str = "") -> None:
        if progress is not None:
            try:
                progress(stage, detail)
            except Exception:
                logger.exception("repair progress observer failed")
    if not str(task.experiment_id or "").strip():
        raise AutoRepairError("task requires experiment_id")
    if not str(task.problem or "").strip():
        raise AutoRepairError("task requires a problem statement")
    attempts_limit = max(1, min(MAX_ATTEMPTS_LIMIT, int(max_attempts or DEFAULT_MAX_ATTEMPTS)))
    roles = _role_services(config, services)
    role_assignments = _role_assignment_summary(roles)
    store = ledger or ReviewLedger()
    if repo_path is None:
        from core.config import BASE_DIR
        repo_path = Path(BASE_DIR)

    notes: List[str] = []
    coding_provider, coding_model = roles["coding"]
    if coding_provider is None:
        return {"outcome": OUTCOME_NO_PROVIDER, "experiment_id": task.experiment_id,
                "attempts": [], "attempts_used": 0, "notes":
                ["no AI provider enabled — enable one in AI settings (§37) to run the coding agent"],
                "role_assignments": role_assignments}
    if coding_model and coding_model in {roles["code_review"][1], roles["security_review"][1]}:
        notes.append(
            "transparency: the same model serves the coding agent and a reviewer role — "
            "assign an independent critic model for stricter §81 separation (§38)"
        )

    feedback: List[str] = []
    attempts: List[Dict[str, Any]] = []
    outcome = OUTCOME_EXHAUSTED
    final_submission: Optional[Dict[str, Any]] = None
    final_base_tree: Optional[str] = None

    for attempt_number in range(1, attempts_limit + 1):
        attempt_id = f"{task.experiment_id}-R{attempt_number}"
        record: Dict[str, Any] = {"attempt": attempt_number, "submission_id": None,
                                  "status": None, "stages": {}, "worktree_removed": False,
                                  "started_at": _utc_now(), "finished_at": None,
                                  "events": [],
                                  "provider": role_assignments["coding"]["provider"],
                                  "model": role_assignments["coding"]["model"]}
        attempts.append(record)

        # 1) coding agent generates (or regenerates, given feedback)
        _emit("coding", f"attempt {attempt_number}: generating a diff for: "
                        f"{task.problem[:120]}{'…' if len(task.problem) > 120 else ''}")
        _attempt_event(record, "CODING", "STARTED", "Generating a bounded unified diff.")
        try:
            diff = generate_repair_diff(task, feedback, roles=roles)
        except AutoRepairError as exc:
            feedback.append(f"attempt {attempt_number}: {exc}")
            record["error"] = str(exc)
            if "no code defect" in str(exc):
                record["status"] = "NO_DEFECT"
                outcome = OUTCOME_NO_DEFECT
                _attempt_event(record, "CODING", "NO_DEFECT", str(exc))
                record["finished_at"] = _utc_now()
                break
            record["status"] = "GENERATION_FAILED"
            _attempt_event(record, "CODING", "FAILED", str(exc))
            record["finished_at"] = _utc_now()
            if "unavailable" in str(exc):
                outcome = OUTCOME_GENERATION_UNAVAILABLE
                break
            continue

        _attempt_event(record, "CODING", "PASSED",
                       f"Generated a parseable diff ({len(diff)} characters).")

        # canonicalize LLM diff quirks (new-file form, artifact lines) so the
        # model's intent survives git apply; policy sees the canonical form too
        diff = normalize_diff_for_apply(diff, Path(repo_path))

        scope_findings = repair_scope_findings(task, diff, Path(repo_path))
        if scope_findings:
            feedback.append(
                f"attempt {attempt_number}: incident-scope guard rejected — "
                + "; ".join(scope_findings)
            )
            record["status"] = "SCOPE_REJECTED"
            record["error"] = "; ".join(scope_findings)
            _attempt_event(record, "SCOPE_GUARD", "FAILED", record["error"])
            record["finished_at"] = _utc_now()
            continue
        _attempt_event(record, "SCOPE_GUARD", "PASSED", "Diff stayed inside the incident scope.")

        # pre-flight honesty: surface policy findings before burning a worktree
        findings = review_diff(diff)
        if findings:
            feedback.append(
                f"attempt {attempt_number}: policy guard would reject — "
                + "; ".join(f"{f.rule}:{f.path}" for f in findings)
            )
            _attempt_event(
                record, "POLICY_GUARD", "WARNING",
                "; ".join(f"{f.rule}: {f.path}" for f in findings),
            )
        else:
            _attempt_event(record, "POLICY_GUARD", "PASSED", "No pre-flight policy findings.")

        # 2) §112 pipeline: policy guard → worktree apply → ledger open
        try:
            result = submit_change(
                repo_path=Path(repo_path), experiment_id=attempt_id,
                author=AUTHOR_ACTOR, diff_text=diff, ledger=store,
                worktree_parent=worktree_parent,
            )
        except DevelopmentPipelineError as exc:
            entry = f"attempt {attempt_number}: {exc}"
            if "corrupt patch" in str(exc) or "truncated" in str(exc):
                entry += (" — your diff was TRUNCATED mid-line (output token limit): "
                          "produce a MUCH shorter diff (target < 60 total lines, "
                          "one-line docstrings, no comments)")
            feedback.append(entry)
            record["status"] = "PIPELINE_REJECTED"
            record["error"] = str(exc)
            # keep the rejected diff with the attempt — anchor failures are
            # debuggable only with the actual hunks, not the git error alone
            record["rejected_diff"] = str(diff)[:4000]
            remove_worktree(Path(repo_path), attempt_id)  # best-effort sandbox cleanup
            record["worktree_removed"] = True
            _attempt_event(record, "SANDBOX", "FAILED", entry)
            record["finished_at"] = _utc_now()
            continue
        submission = result["submission"]
        worktree_path = Path(result["worktree"]["path"])
        record.update({
            "submission_id": submission["submission_id"],
            "status": submission["status"],
            "diff_sha256": submission["diff_sha256"],
            "worktree": str(worktree_path),
            "base_tree": result["worktree"].get("base_tree"),
        })
        _attempt_event(
            record, "SANDBOX", "PASSED", "Diff accepted and applied in an isolated worktree.",
            {"submission_id": submission["submission_id"],
             "diff_sha256": submission["diff_sha256"]},
        )

        # 3) independent reviews; any NOT_PERFORMED stops the loop honestly (§81)
        review_blocked = False
        for stage in ("CODE_REVIEW", "SECURITY_REVIEW"):
            _emit("review", f"attempt {attempt_number}: independent {stage} in progress")
            verdict, evidence = run_llm_review(stage, task, diff, roles=roles)
            store.record_review(submission["submission_id"], stage=stage,
                                actor=STAGE_ACTORS[stage], verdict=verdict,
                                evidence=evidence)
            record["stages"][stage] = verdict
            _attempt_event(record, stage, verdict,
                           str(evidence.get("rationale") or ""), evidence)
            if verdict == "NOT_PERFORMED":
                review_blocked = True
                break
            if verdict == "FAILED":
                joined = "; ".join(evidence.get("findings") or []) or evidence.get("rationale", "")
                feedback.append(f"attempt {attempt_number}: {stage} FAILED — {joined[:400]}")
                break
        if review_blocked:
            outcome = OUTCOME_REVIEW_UNAVAILABLE
            record["status"] = store.get(submission["submission_id"]).status
            remove_worktree(Path(repo_path), attempt_id)
            record["worktree_removed"] = True
            record["finished_at"] = _utc_now()
            break
        if record["stages"].get("CODE_REVIEW") == "FAILED" or \
           record["stages"].get("SECURITY_REVIEW") == "FAILED":
            record["status"] = store.get(submission["submission_id"]).status  # REJECTED
            remove_worktree(Path(repo_path), attempt_id)
            record["worktree_removed"] = True
            record["finished_at"] = _utc_now()
            continue

        # 4) deterministic TESTS stage (test_agent)
        _emit("testing", f"attempt {attempt_number}: deterministic sandbox tests")
        verdict, evidence = run_sandbox_tests(worktree_path, diff)
        store.record_review(submission["submission_id"], stage="TESTS",
                            actor=STAGE_ACTORS["TESTS"], verdict=verdict, evidence=evidence)
        record["stages"]["TESTS"] = verdict
        _attempt_event(record, "TESTS", verdict,
                       str(evidence.get("note") or ""), evidence)
        if verdict == "FAILED":
            feedback.append(
                f"attempt {attempt_number}: TESTS FAILED — "
                + "; ".join(evidence.get("failures") or [])[:400]
            )
            record["status"] = store.get(submission["submission_id"]).status  # REJECTED
            remove_worktree(Path(repo_path), attempt_id)
            record["worktree_removed"] = True
            record["finished_at"] = _utc_now()
            continue

        # survived CODE_REVIEW + SECURITY_REVIEW + TESTS: Phase 15 boundary reached
        record["status"] = store.get(submission["submission_id"]).status  # OPEN
        final_submission = store.get(submission["submission_id"]).to_dict()
        final_base_tree = record.get("base_tree")
        outcome = OUTCOME_ADVANCED
        _attempt_event(record, "REVIEW_CHAIN", "PASSED",
                       "Coding, policy, sandbox, independent reviews and tests passed.")
        record["finished_at"] = _utc_now()
        _emit("advanced", "chain passed — applying the reviewed fix")
        notes.append(
            "QUANT_VALIDATION and E2E remain unrecorded — they belong to the "
            "quant_validator/e2e_agent actors (Phase 16/17), never to the author"
        )
        break

    return {
        "outcome": outcome,
        "experiment_id": task.experiment_id,
        "attempts_used": len(attempts),
        "attempts": attempts,
        "final_submission": final_submission,
        "base_tree": final_base_tree,
        "feedback_history": feedback,
        "notes": notes,
        "sandbox_only": True,
        "role_assignments": role_assignments,
    }


# ------------------------------------------------- experiment → task bridging

def build_task_from_experiment(
    experiment_id: str, *,
    registry=None, hypothesis_store=None,
) -> RepairTask:
    """Build the coding-agent task from a §77 experiment (+ its §76 hypothesis)."""
    from core.research_factory.experiments import ExperimentRegistry
    from core.research_factory.hypothesis import HypothesisStore

    reg = registry or ExperimentRegistry(hypothesis_store=hypothesis_store or HypothesisStore())
    experiment = reg.get(str(experiment_id).strip())
    if experiment is None:
        raise AutoRepairError(f"unknown experiment_id: {experiment_id}")
    hypothesis = None
    if reg.hypothesis_store is not None:
        hypothesis = reg.hypothesis_store.get(experiment.hypothesis_id)
    context: Dict[str, Any] = {
        "experiment": {
            "experiment_id": experiment.experiment_id,
            "strategy_id": experiment.strategy_id,
            "version": experiment.version,
            "status": experiment.status,
            "description": experiment.description,
            "evidence": experiment.evidence,
        },
    }
    problem = experiment.description
    if hypothesis is not None:
        context["hypothesis"] = hypothesis.to_dict()
        problem = (
            f"{hypothesis.problem}\nObservation: {hypothesis.observation}\n"
            f"Hypothesis: {hypothesis.hypothesis}\nExpected mechanism: {hypothesis.expected_mechanism}\n"
            f"Baseline: {hypothesis.baseline}\nChallenger: {hypothesis.challenger}"
        )
    return RepairTask(
        experiment_id=experiment.experiment_id,
        problem=problem,
        target_hint=f"strategy_id={experiment.strategy_id} version={experiment.version}",
        context=context,
    )


__all__ = [
    "AutoRepairError", "RepairTask", "run_auto_repair", "generate_repair_diff",
    "run_llm_review", "run_sandbox_tests", "build_task_from_experiment",
    "extract_diff", "auto_repair_config", "read_context_files",
    "normalize_diff_for_apply", "repair_scope_findings", "AUTHOR_ACTOR",
]
