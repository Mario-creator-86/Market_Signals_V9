"""§112 continuous — the autonomous repair watchdog (detect → fix → verify).

Non-stop detector agents find problems (backtest anomalies via the §111
research evidence, runtime/scheduler errors). Every case walks the full agent
chain: coding_agent proposes a diff → policy guard + sandbox (§80/§82/§83/§84)
→ independent code_review_agent + security_review_agent + deterministic
test_agent (§81). When the review chain PASSES, the repair is applied
AUTOMATICALLY (owner policy: the independent reviewers' approval is the
approval) followed by a post-apply sanity check; a failing check rolls the
change back automatically. A problem with no code-level cause is closed
honestly as CLOSED_NO_DEFECT — never faked. The whole journey is visible on
the cockpit card, case by case, agent by agent.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.development_factory.auto_repair import (
    AUTHOR_ACTOR, AutoRepairError, RepairTask, auto_repair_config,
    read_context_files, run_auto_repair,
)
from core.development_factory.review import ReviewLedger
from core.development_factory.sandbox import (
    SandboxError, apply_unified_diff, remove_worktree, run_bounded,
)

logger = logging.getLogger(__name__)

from core.config import DATA_DIR  # noqa: E402

# In the configured data folder (MS_DATA_DIR), not relative to the working directory.
DEFAULT_CASES_PATH = DATA_DIR / "auto_repair_cases.json"
DEFAULT_ACTIVITY_PATH = DATA_DIR / "auto_repair_activity.json"
SCHEMA_VERSION = 1

# coding-agent live states for the owner's tab
ACTIVITY_WORKING = "WORKING"            # chain running right now
ACTIVITY_IDLE = "IDLE"                  # nothing to do — detectors keep watching
ACTIVITY_WAITING_OWNER = "WAITING_OWNER"  # proposals need the owner's APPROVE

# case lifecycle: DETECTED -> IN_PROGRESS -> {APPLIED | FAILED | ROLLED_BACK |
# CLOSED_NO_DEFECT | PROPOSED}; PROPOSED (improvements) waits for the owner's
# APPROVE -> APPLIED; DISMISSED is the owner's manual close.
OPEN_STATUSES = ("DETECTED", "IN_PROGRESS")
TERMINAL_STATUSES = ("APPLIED", "FAILED", "ROLLED_BACK", "CLOSED_NO_DEFECT",
                     "PROPOSED", "OWNER_ACTION", "RESOLVED", "DISMISSED")
# health tiles that are not-configured BY DESIGN — never owner actions
INTENTIONALLY_UNCONFIGURED = {"live broker"}
FINGERPRINT_COOLDOWN_HOURS = 6.0
IMPROVEMENT_COOLDOWN_HOURS = 12.0
MAX_CASES_PER_CYCLE = 2
MAX_STATIC_DRAFTS = 3

DEFAULT_WATCHDOG_INTERVAL = 15 * 60
MIN_WATCHDOG_INTERVAL = 5 * 60


class WatchdogError(ValueError):
    """Bounded, caller-facing error (422-class)."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_cases_path() -> Path:
    override = os.environ.get("MS_AUTO_REPAIR_CASES_PATH", "").strip()
    return Path(override) if override else DEFAULT_CASES_PATH


def default_activity_path() -> Path:
    override = os.environ.get("MS_AUTO_REPAIR_ACTIVITY_PATH", "").strip()
    return Path(override) if override else DEFAULT_ACTIVITY_PATH


_ACTIVITY_LOCK = threading.RLock()

_OUTCOME_COUNTERS = {
    "APPLIED": "applied", "FAILED": "failed", "ROLLED_BACK": "rolled_back",
    "CLOSED_NO_DEFECT": "no_defect", "PROPOSED": "proposals",
}


def record_activity(state: str, *, case_id: str = "", title: str = "",
                    stage: str = "", detail: str = "",
                    outcome: str = "", path: Optional[Path] = None) -> Dict[str, Any]:
    """Append the coding agent's live state (what it does / thinks, per stage).

    `started_at` marks entry into the CURRENT state (idle timer anchor);
    `last_finished_at` marks the last time a case reached a terminal state.
    """
    target = Path(path) if path else default_activity_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _ACTIVITY_LOCK:
        current: Dict[str, Any] = {}
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(current, dict):
                current = {}
        except (OSError, ValueError):
            pass
        now = _utc_now()
        counters = dict(current.get("counters") or {})
        if outcome in _OUTCOME_COUNTERS:
            counters[_OUTCOME_COUNTERS[outcome]] = int(counters.get(_OUTCOME_COUNTERS[outcome], 0)) + 1
        entered = now if state != current.get("state") else (current.get("started_at") or now)
        record = {
            "state": state,
            "case_id": case_id or current.get("case_id", ""),
            "title": title or current.get("title", ""),
            "stage": stage or current.get("stage", ""),
            "detail": detail or current.get("detail", ""),
            "started_at": entered,
            "updated_at": now,
            "last_finished_at": (now if outcome else current.get("last_finished_at")) or "",
            "last_outcome": outcome or current.get("last_outcome", ""),
            "counters": counters,
        }
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return record


def activity_status(path: Optional[Path] = None) -> Dict[str, Any]:
    """Live agent state + honest idle/duration bookkeeping for the tab."""
    target = Path(path) if path else default_activity_path()
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or not record.get("state"):
            raise ValueError
    except (OSError, ValueError):
        return {
            "state": ACTIVITY_IDLE, "case_id": "", "title": "", "stage": "",
            "detail": "the coding agent has not worked yet — detectors watch continuously",
            "started_at": "", "updated_at": "", "last_finished_at": "",
            "last_outcome": "", "counters": {},
            "in_state_seconds": None, "idle_seconds": None, "ever_worked": False,
        }
    from datetime import datetime as _dt

    def _age(stamp: str) -> Optional[float]:
        try:
            return max(0.0, (datetime.now(timezone.utc) - _dt.fromisoformat(stamp)).total_seconds())
        except (TypeError, ValueError):
            return None

    state = record.get("state")
    return {
        **record,
        "in_state_seconds": _age(str(record.get("started_at") or "")),
        "idle_seconds": _age(str(record.get("updated_at") or "")) if state != ACTIVITY_WORKING else None,
        "ever_worked": bool(record.get("last_finished_at")),
    }


# --------------------------------------------------------------- case store

@dataclass(frozen=True)
class CaseDraft:
    """A detector's finding, before it becomes a case."""
    fingerprint: str
    kind: str                    # BACKTEST | RUNTIME
    source_agent: str
    title: str
    problem: str
    context: Dict[str, Any] = field(default_factory=dict)


def _fingerprint(*parts: str) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


class RepairCaseStore:
    """Atomic JSON store of repair cases with an append-only history."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else default_cases_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict) and isinstance(payload.get("cases"), list):
                return payload
        except (OSError, ValueError):
            pass
        return {"schema_version": SCHEMA_VERSION, "cases": []}

    def _save(self, payload: Dict[str, Any]) -> None:
        import tempfile

        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            for attempt in range(6):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def enqueue(self, draft: CaseDraft) -> Optional[Dict[str, Any]]:
        """Add a DETECTED case unless the same fingerprint is open or cooling down."""
        with self._lock:
            payload = self._load()
            cases = payload["cases"]
            now = time.time()
            for case in cases:
                if case.get("fingerprint") != draft.fingerprint:
                    continue
                if case.get("status") in OPEN_STATUSES:
                    return None
                closed = case.get("closed_at") or case.get("updated_at") or ""
                try:
                    age_h = (datetime.now(timezone.utc)
                             - datetime.fromisoformat(closed)).total_seconds() / 3600
                except ValueError:
                    age_h = 1e9
                if age_h < FINGERPRINT_COOLDOWN_HOURS:
                    return None
            case = {
                "case_id": f"case_{uuid.uuid4().hex[:10]}",
                "fingerprint": draft.fingerprint,
                "kind": draft.kind,
                "source_agent": draft.source_agent,
                "title": draft.title,
                "problem": draft.problem,
                "context": dict(draft.context),
                "status": "DETECTED",
                "detected_at": _utc_now(),
                "updated_at": _utc_now(),
                "attempts": [],
                "verdicts": [],
                "history": [{"at": _utc_now(), "from": "", "to": "DETECTED",
                             "note": f"detected by {draft.source_agent}"}],
                "submission_id": None,
                "diff_sha256": None,
                "diff_text": None,
                "applied_files": [],
                "applied_at": None,
                "post_check": None,
                "outcome": None,
                "error": None,
                "closed_at": None,
            }
            cases.append(case)
            self._save(payload)
            return case

    def get(self, case_id: str) -> Optional[Dict[str, Any]]:
        for case in self._load()["cases"]:
            if case.get("case_id") == case_id:
                return case
        return None

    def list_cases(self, *, limit: int = 50, status: Optional[str] = None, order: str = "DESC") -> List[Dict[str, Any]]:
        cases = list(self._load()["cases"])
        if status:
            cases = [c for c in cases if c.get("status") == status.upper()]
        if order.upper() == "ASC":
            return cases[:max(1, min(200, int(limit or 50)))]
        return list(reversed(cases[-max(1, min(200, int(limit or 50))):]))

    def update(self, case_id: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            payload = self._load()
            for index, case in enumerate(payload["cases"]):
                if case.get("case_id") != case_id:
                    continue
                transition = None
                if "status" in fields and fields["status"] != case.get("status"):
                    transition = {"at": _utc_now(), "from": case.get("status"),
                                  "to": fields["status"], "note": str(fields.pop("_note", ""))}
                case.update({k: v for k, v in fields.items() if not k.startswith("_")})
                case["updated_at"] = _utc_now()
                if case.get("status") in TERMINAL_STATUSES and not case.get("closed_at"):
                    case["closed_at"] = _utc_now()
                if transition:
                    case.setdefault("history", []).append(transition)
                payload["cases"][index] = case
                self._save(payload)
                return case
        raise WatchdogError(f"unknown case_id: {case_id}")


# ------------------------------------------------------------------ detectors

def detect_backtest_anomalies(
    research_path: Optional[Path] = None,
    performance_path: Optional[Path] = None,
) -> List[CaseDraft]:
    """quant_validator watch: suspended/degraded strategies.

    Two real evidence sources, both keyed `symbol|asset|strategy|direction`:
    the closed-trade performance cache (feedback_loop marks degraded at
    win_rate < 40% over >= 20 trades and suspended at profit_factor < 1.0
    over >= 30 trades) and the research walk-forward cache. MANUAL/MANUAL_CHART
    rows are the owner's own trades — never repair targets.
    """
    from core.strategy_v2.performance_cache import (
        STRATEGY_PERF_CACHE_PATH, STRATEGY_RESEARCH_CACHE_PATH,
    )

    candidates: Dict[str, Dict[str, Any]] = {}  # strategy_id -> latest flagged row
    for path in (Path(performance_path) if performance_path else STRATEGY_PERF_CACHE_PATH,
                 Path(research_path) if research_path else STRATEGY_RESEARCH_CACHE_PATH):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries = data.get("entries") if isinstance(data, dict) else data
        if not isinstance(entries, dict):
            continue
        for key, value in entries.items():
            if not isinstance(value, dict):
                continue
            parts = str(key).split("|")
            if len(parts) < 4:
                continue
            strategy_id = parts[2].upper()
            if strategy_id.startswith("MANUAL"):
                continue  # owner's manual trades, not autonomous strategy code
            if not (value.get("suspended") or value.get("degraded")):
                continue
            stamp = str(value.get("validation_as_of") or value.get("last_run")
                        or value.get("updated_at") or "")
            prev = candidates.get(strategy_id)
            if prev is None or stamp > prev[0]:
                candidates[strategy_id] = (stamp, dict(value))

    drafts: List[CaseDraft] = []
    for strategy_id, (_stamp, row) in sorted(candidates.items()):
        evidence = {
            "strategy_id": strategy_id,
            "suspended": bool(row.get("suspended")),
            "degraded": bool(row.get("degraded")),
            "validation_status": row.get("validation_status"),
            "expectancy_R": row.get("expectancy_R"),
            "win_rate": row.get("win_rate"),
            "profit_factor": row.get("profit_factor"),
            "sample_size": row.get("sample_size"),
            "validation_as_of": row.get("validation_as_of")
            or row.get("last_run") or row.get("updated_at"),
        }
        problem = (
            f"Backtest watcher: strategy {strategy_id} is flagged "
            f"{'SUSPENDED' if row.get('suspended') else 'DEGRADED'} by the live "
            f"performance evidence (win_rate={row.get('win_rate')}, "
            f"profit_factor={row.get('profit_factor')}, "
            f"expectancy_R={row.get('expectancy_R')}, sample={row.get('sample_size')}, "
            f"as_of={evidence['validation_as_of']}). Investigate whether the cause "
            f"is a CODE-LEVEL defect in the strategy implementation or its "
            f"evaluation (core/strategy_v2/: registry.py, evaluators.py, "
            f"risk_manager.py, quality_gates.py) — e.g. wrong signal logic, wrong "
            f"cost/slippage application, look-ahead, or a crash path. Fix ONLY a "
            f"confirmed code defect with a minimal diff; never weaken costs, gates "
            f"or golden tests. If the evidence shows no code-level cause, answer "
            f"NO_CODE_DEFECT."
        )
        drafts.append(CaseDraft(
            fingerprint=_fingerprint("backtest", strategy_id,
                                     "suspended" if row.get("suspended") else "degraded"),
            kind="BACKTEST", source_agent="quant_validator",
            title=f"{strategy_id} {'suspended' if row.get('suspended') else 'degraded'} in live performance evidence",
            problem=problem, context={"evidence": evidence},
        ))
    return drafts


def detect_runtime_errors(config: Any = None) -> List[CaseDraft]:
    """runtime_monitor watch: daemons that crash OR that should run but don't."""
    drafts: List[CaseDraft] = []
    probes = (
        (
            "research_cycle", "Research auto-cycle (§111)",
            "core.research_factory.scheduler", "get_research_scheduler",
            [
                "core/research_factory/scheduler.py",
                "core/research_factory/pipeline.py",
                "core/research_factory/tracking.py",
                "api/research_factory_service.py",
            ],
        ),
        (
            "pre_earnings_scan", "Continuous Pre-Earnings scanner",
            "core.pre_earnings.continuous", "get_continuous_scanner",
            [
                "core/pre_earnings/continuous.py",
                "api/research_factory_service.py",
            ],
        ),
    )
    for name, label, module_path, factory, allowed_files in probes:
        running, last_error, should_run = False, "", False
        try:
            module = __import__(module_path, fromlist=[factory])
            status = getattr(module, factory)(None).status()
            running = bool(status.get("running"))
            last_error = str(status.get("last_error") or "")
        except Exception:
            continue
        # "the system must do something and does not" — config says run, daemon isn't
        if not running:
            try:
                if config is None:
                    from api.state_service import get_shared_config
                    config = get_shared_config()
            except Exception:
                config = None
            root = getattr(config, "config", config) if config is not None else {}
            section_root = ((root or {}).get("research_factory") or {}).get("auto_cycle") \
                if name == "research_cycle" \
                else ((root or {}).get("pre_earnings") or {}).get("continuous_scan")
            should_run = bool((section_root or {}).get("enabled", False)) if isinstance(section_root, dict) else False
            if name == "research_cycle" and isinstance(root, dict):
                # The Agent Fleet toggle is the operator-facing source of truth.
                # A legacy split-brain config (agent ON, scheduler OFF) must be
                # detected instead of being misclassified as an intentional stop.
                agents = root.get("agents") or {}
                if isinstance(agents, dict) and "research_factory" in agents:
                    agent_value = agents.get("research_factory")
                    if isinstance(agent_value, dict):
                        should_run = bool(agent_value.get("enabled", False))
                    elif isinstance(agent_value, bool):
                        should_run = agent_value
        if running and last_error:
            problem = (
                f"Runtime monitor: the {label} daemon reports a persistent error: "
                f"{last_error[:500]}. Investigate the implementing module for a "
                f"code-level defect (crash path, bad data handling, unhandled edge "
                f"case) and fix it with a minimal diff. If the error is environmental "
                f"(network/provider/data unavailable) and not a code defect, answer "
                f"NO_CODE_DEFECT."
            )
            drafts.append(CaseDraft(
                fingerprint=_fingerprint("runtime", name, last_error[:120]),
                kind="RUNTIME", source_agent="runtime_monitor",
                title=f"{label} error: {last_error[:80]}",
                problem=problem,
                context={
                    "subsystem": name,
                    "last_error": last_error[:1000],
                    "file": allowed_files[0],
                    "allowed_files": allowed_files,
                },
            ))
        elif should_run and not running:
            drafts.append(CaseDraft(
                fingerprint=_fingerprint("runtime", name, "should-run-not-running"),
                kind="RUNTIME", source_agent="runtime_monitor",
                title=f"{label} is enabled but NOT running",
                problem=(
                    f"Runtime monitor: the owner's config enables the {label} daemon "
                    f"but it is not running (it must run while the system runs). "
                    f"Investigate why it is stopped — a crashed loop, a start-up "
                    f"exception swallowed at lifespan, or a stop that never "
                    f"restarted — and fix the code-level cause with a minimal diff. "
                    f"If the daemon is intentionally stopped by the owner, answer "
                    f"NO_CODE_DEFECT."
                ),
                context={
                    "subsystem": name,
                    "enabled_but_running": False,
                    "agent_fleet_intent": should_run,
                    "file": allowed_files[0],
                    "allowed_files": allowed_files,
                },
            ))
    return drafts


def detect_drift_cases(trades: Optional[List[Dict[str, Any]]] = None) -> List[CaseDraft]:
    """§79 drift monitor as a repair source: DRIFT_SUSPECTED strategies only
    (STABLE and INSUFFICIENT_SAMPLE never open cases)."""
    try:
        from core.research_factory.drift import detect_drift
        from core.research_factory.weakness import load_closed_trades

        reports = detect_drift(trades if trades is not None else load_closed_trades())
    except Exception:
        logger.exception("drift detection crashed")
        return []
    drafts: List[CaseDraft] = []
    for report in reports:
        if report.verdict != "DRIFT_SUSPECTED":
            continue
        metrics = report.metrics or {}
        drafts.append(CaseDraft(
            fingerprint=_fingerprint("drift", report.strategy_id, report.direction),
            kind="BACKTEST", source_agent="quant_validator",
            title=f"{report.strategy_id} {report.direction}: concept drift suspected",
            problem=(
                f"Drift monitor (§79): strategy {report.strategy_id} "
                f"{report.direction} shows DRIFT_SUSPECTED — the recent window "
                f"degraded versus the reference window "
                f"(recent={metrics.get('recent')} reference={metrics.get('reference')} "
                f"win_rate_drop={metrics.get('win_rate_drop')} "
                f"expectancy_ratio={metrics.get('expectancy_ratio')}). Investigate "
                f"whether a CODE-LEVEL defect explains the shift (signal logic, "
                f"regime handling, cost application in core/strategy_v2/ and its "
                f"evaluation) and fix it with a minimal diff; never weaken costs, "
                f"gates or golden tests. If the drift is market-side (the strategy "
                f"stopped matching this regime), answer NO_CODE_DEFECT."
            ),
            context={"evidence": {"strategy_id": report.strategy_id,
                                  "direction": report.direction,
                                  **{k: v for k, v in metrics.items() if isinstance(v, (int, float, str)) or v is None}}},
        ))
    return drafts


def _fetch_cockpit_payload() -> Optional[Dict[str, Any]]:
    """Self-probe the cockpit snapshot (None when unreachable)."""
    import urllib.request

    port = os.environ.get("MS_PORT", "8003")
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/cockpit", timeout=30,
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


# exact activation steps surfaced to the owner per tile
_OWNER_ACTION_GUIDANCE = {
    "sec": "Set EDGAR_IDENTITY in the .env file at the repository root "
           "(format: 'Name <email>'). The SEC requires an identifiable user; "
           "the system is not allowed to invent one. Restart the server after.",
    "ml model": "The ML registry (config ml.registry) is empty because no real "
                "training run has been executed yet. Training code exists "
                "(core/ml/: train_baseline, calibration, meta-labeling). Use the "
                "'Train ML Model' button on the ML model tile in Cockpit → "
                "System Health; it needs enough closed trades to train on.",
    "mlflow": "MLflow is configured with auto-start on http://127.0.0.1:5001. "
              "If unreachable, ensure MLFLOW_TRACKING_URI is set in .env and restart.",
    "rd-agent worker": "Set RD_AGENT_ENABLED=1 in .env to enable the RD-Agent "
                       "worker subprocess manager. Requires Python 3.10.",
    "options": "No IV/options evidence is present in the current earnings "
               "snapshot. Options data appears when the data provider supplies "
               "it for the analyzed tickers; verify the provider keys in .env.",
}


def detect_owner_actions(payload: Optional[Dict[str, Any]] = None) -> List[CaseDraft]:
    """Owner-action watch: capabilities that SHOULD be configured but aren't.

    NOT_CONFIGURED / UNAVAILABLE health tiles (except the intentionally
    unavailable Live Broker) are owner TODOs — credentials, training runs,
    external tooling. The coding agent cannot fix a missing credential, so
    these surface as OWNER_ACTION cases for the owner, never repair cases.
    """
    if payload is None:
        payload = _fetch_cockpit_payload()
    if not isinstance(payload, dict):
        return []
    drafts: List[CaseDraft] = []
    for service in payload.get("system_health", {}).get("services", []) or []:
        label = str(service.get("label") or "").strip()
        status = str(service.get("status") or "").upper()
        if label.lower() in INTENTIONALLY_UNCONFIGURED:
            continue
        if status not in {"NOT_CONFIGURED", "UNAVAILABLE"}:
            continue
        message = str(service.get("message") or "")
        guidance = _OWNER_ACTION_GUIDANCE.get(label.lower(),
                                              message or f"The {label} capability is {status}.")
        drafts.append(CaseDraft(
            fingerprint=_fingerprint("owner-action", label),
            kind="OWNER_ACTION", source_agent="runtime_monitor",
            title=f"{label} is {status} — owner action needed",
            problem=guidance,
            context={"tile": label, "tile_status": status,
                     "guidance": guidance},
        ))
    return drafts


def detect_cockpit_section_errors(payload: Optional[Dict[str, Any]] = None) -> List[CaseDraft]:
    """cockpit_monitor watch: every cockpit tab/section must render real state;
    HTTP failure or any ERROR section opens a case."""
    if payload is None:
        import urllib.request

        port = os.environ.get("MS_PORT", "8003")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/cockpit", timeout=30,
            ) as response:
                import json as _json
                payload = _json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            return [CaseDraft(
                fingerprint=_fingerprint("cockpit", "payload-unreadable"),
                kind="RUNTIME", source_agent="cockpit_monitor",
                title="Cockpit payload endpoint failed",
                problem=(
                    f"Cockpit monitor: GET /api/cockpit failed ({exc}). The cockpit "
                    f"is the owner's primary view — investigate the code-level cause "
                    f"(crashing section builder, serialization error) and fix it with "
                    f"a minimal diff. If the app is intentionally stopped, answer "
                    f"NO_CODE_DEFECT."
                ),
                context={"error": str(exc)[:500]},
            )]
    drafts: List[CaseDraft] = []
    for name, section in (payload.get("sections") or {}).items():
        if not isinstance(section, dict) or section.get("state") != "ERROR":
            continue
        message = str(section.get("message") or "section in ERROR state")
        drafts.append(CaseDraft(
            fingerprint=_fingerprint("cockpit", name, message[:100]),
            kind="RUNTIME", source_agent="cockpit_monitor",
            title=f"Cockpit section '{name}' in ERROR",
            problem=(
                f"Cockpit monitor: the '{name}' section renders in ERROR state "
                f"({message[:300]}). Investigate the section's data source for a "
                f"code-level defect (crashed service call, broken read model, "
                f"serialization bug) and fix it with a minimal diff so the tab "
                f"renders real state again. If the underlying data source is simply "
                f"unavailable in this environment, answer NO_CODE_DEFECT."
            ),
            context={"section": name, "message": message[:500]},
        ))
    return drafts


def _pyflakes_issues(roots: List[Path]) -> Dict[str, List[str]]:
    """Run pyflakes over `roots`; return per-file issue lists (empty dict when
    pyflakes is unavailable — the caller falls back to syntax-only)."""
    from pyflakes.api import checkPath

    class _Collector:
        def __init__(self) -> None:
            self.files: Dict[str, List[str]] = {}

        def flake(self, message) -> None:
            self.files.setdefault(str(message.filename).replace("\\", "/"), []).append(
                f"line {message.lineno}: {type(message).__name__}: "
                f"{message.message % message.message_args}")

        def unexpectedError(self, filename, msg) -> None:  # noqa: N802
            self.files.setdefault(str(filename).replace("\\", "/"), []).append(f"unexpected: {msg}")

        def syntaxError(self, filename, msg, lineno, offset, text) -> None:  # noqa: N802
            self.files.setdefault(str(filename).replace("\\", "/"), []).append(
                f"line {lineno}: SyntaxError: {msg}")

    issues: Dict[str, List[str]] = {}
    for root in roots:
        for path in sorted(Path(root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            collector = _Collector()
            checkPath(str(path), collector)
            if collector.files:
                issues.update(collector.files)
    return issues


def detect_static_code_issues(roots: Optional[List[Path]] = None) -> List[CaseDraft]:
    """code_quality monitor: pyflakes (undefined names, real bugs) when available,
    syntax-only py_compile fallback otherwise — the evidence says which ran."""
    from core.config import BASE_DIR

    scan_roots = [Path(p) for p in (roots or [BASE_DIR / "core", BASE_DIR / "api", BASE_DIR / "agents"])]
    issues: Dict[str, List[str]] = {}
    checker = "pyflakes"
    try:
        issues = _pyflakes_issues(scan_roots)
    except ImportError:
        checker = "builtin compile (syntax only — install pyflakes for deeper checks)"
        for root in scan_roots:
            for path in sorted(Path(root).rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                try:
                    # compile() on the source string: no .pyc writes and no
                    # os.devnull ("nul" on Windows breaks py_compile's cfile check).
                    compile(path.read_text(encoding="utf-8"), str(path), "exec")
                except (SyntaxError, ValueError) as exc:
                    issues.setdefault(str(path).replace("\\", "/"), []).append(str(exc)[:300])
    drafts: List[CaseDraft] = []
    for file, messages in sorted(issues.items())[:MAX_STATIC_DRAFTS]:
        try:
            file_ref = Path(file).resolve().relative_to(Path(BASE_DIR).resolve()).as_posix()
        except ValueError:
            file_ref = str(file).replace("\\", "/")
        drafts.append(CaseDraft(
            fingerprint=_fingerprint("static", file_ref, messages[0][:80]),
            kind="CODE", source_agent="code_quality_monitor",
            title=f"Static issues in {file_ref.split('/')[-1]} ({len(messages)})",
            problem=(
                f"Static code monitor ({checker}) reports {len(messages)} issue(s) "
                f"in {file_ref}:\n" + "\n".join(f"- {m}" for m in messages[:8]) +
                f"\nInvestigate whether they reveal a real defect (undefined name, "
                f"shadowed import, dead branch) and fix the confirmed ones with a "
                f"minimal diff. Style-only findings that do not change behavior "
                f"should be answered with NO_CODE_DEFECT."
            ),
            context={"file": file_ref, "checker": checker, "issues": messages[:20]},
        ))
    return drafts


def detect_ui_asset_issues(frontend_dir: Optional[Path] = None) -> List[CaseDraft]:
    """ui_monitor watch: every frontend module must parse (node --check) — a
    syntax error breaks a whole tab; honest skip when node is unavailable."""
    import shutil

    from core.config import BASE_DIR

    if shutil.which("node") is None:
        return []
    directory = Path(frontend_dir) if frontend_dir else BASE_DIR / "frontend"
    drafts: List[CaseDraft] = []
    for path in sorted(directory.glob("*.js")):
        check = subprocess.run(
            ["node", "--check", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if check.returncode != 0:
            detail = (check.stderr or "").strip().splitlines()
            message = detail[-1][:200] if detail else "syntax error"
            drafts.append(CaseDraft(
                fingerprint=_fingerprint("ui", path.name, message[:80]),
                kind="CODE", source_agent="ui_monitor",
                title=f"frontend/{path.name} fails to parse",
                problem=(
                    f"UI monitor: node --check reports a syntax error in "
                    f"frontend/{path.name} ({message}) — a broken module can take "
                    f"an entire tab down. Fix the syntax error with a minimal diff "
                    f"preserving the file's behaviour."
                ),
                context={"file": f"frontend/{path.name}", "error": message},
            ))
            if len(drafts) >= MAX_STATIC_DRAFTS:
                break
    return drafts


def resolve_owner_actions(store: Optional[RepairCaseStore] = None,
                           payload: Optional[Dict[str, Any]] = None) -> List[str]:
    """Close OWNER_ACTION cases whose capability is no longer NOT_CONFIGURED.

    The owner configured the credential/tooling (e.g. MLFLOW_TRACKING_URI) —
    the case resolves honestly with a RESOLVED status so the card self-cleans.
    """
    store = store or RepairCaseStore()
    if payload is None:
        payload = _fetch_cockpit_payload()
    if not isinstance(payload, dict):
        return []
    still_gapped = {
        str(s.get("label") or "").lower()
        for s in (payload.get("system_health", {}).get("services") or [])
        if str(s.get("status") or "").upper() in {"NOT_CONFIGURED", "UNAVAILABLE"}
    }
    resolved: List[str] = []
    open_actions = [
        *(store.list_cases(limit=50, status="OWNER_ACTION")),
        *(store.list_cases(limit=50, status="DETECTED")),
    ]
    for case in open_actions:
        if case.get("kind") != "OWNER_ACTION":
            continue
        tile = str((case.get("context") or {}).get("tile") or "").lower()
        if tile in still_gapped:
            continue  # the gap is real, keep waiting for the owner
        try:
            store.update(case["case_id"], status="RESOLVED",
                         _note="capability configured — owner action complete")
            resolved.append(case["case_id"])
        except WatchdogError:
            pass
    return resolved


def detect_ui_tab_stalls() -> List[CaseDraft]:
    """ui_inspector watch: inspects every UI tab for missing evidence, empty feeds, or stalls."""
    try:
        from agents.ui_inspector_agent import get_ui_inspector_agent_if_ready
        inspector = get_ui_inspector_agent_if_ready()
        if inspector:
            report = inspector.run_inspection_cycle()
            drafts: List[CaseDraft] = []
            for anom in (report.get("anomalies") or []):
                drafts.append(CaseDraft(
                    fingerprint=_fingerprint("ui_inspector", anom.get("tab", ""), anom.get("title", "")),
                    kind="RUNTIME",
                    source_agent="ui_inspector",
                    title=f"[UI Inspector] {anom.get('title')}",
                    problem=anom.get("problem", "UI Data stall detected."),
                    context=anom.get("context", {}),
                ))
            return drafts
    except Exception:
        pass
    return []


def run_detection(store: Optional[RepairCaseStore] = None,
                  config: Any = None) -> Dict[str, Any]:
    """Run every detector and enqueue new cases (dedup + cooldown)."""
    store = store or RepairCaseStore()
    try:
        drafts = (
            detect_backtest_anomalies()
            + detect_drift_cases()
            + detect_runtime_errors(config)
            + detect_cockpit_section_errors()
            + detect_ui_tab_stalls()
            + detect_static_code_issues()
            + detect_ui_asset_issues()
            + detect_owner_actions()
        )
    except Exception:
        logger.exception("detection cycle crashed")
        drafts = []
    enqueued, skipped = [], 0
    for draft in drafts:
        case = store.enqueue(draft)
        if case:
            enqueued.append(case["case_id"])
        else:
            skipped += 1
    return {"detected": len(drafts), "enqueued": enqueued, "skipped_duplicates": skipped}


# ------------------------------------------------------- apply + post-check

def _diff_files(diff_text: str) -> List[str]:
    return [line[6:].strip() for line in str(diff_text).splitlines()
            if line.startswith("+++ b/")]


def _module_of(path: str) -> str:
    return path[:-3].replace("/", ".").replace("\\", ".")


def post_apply_check(repo_path: Path, diff_text: str) -> Dict[str, Any]:
    """Deterministic sanity after applying: py-compile touched files; import NEW modules."""
    repo = Path(repo_path)
    files = [f for f in _diff_files(diff_text) if f.endswith(".py")]
    failures: List[str] = []
    for rel in files:
        target = repo / rel
        if not target.exists():
            failures.append(f"{rel}: missing after apply")
            continue
        compiled = subprocess.run(
            [sys.executable, "-m", "py_compile", str(target)],
            capture_output=True, text=True, timeout=60,
        )
        if compiled.returncode != 0:
            detail = (compiled.stderr or "").strip().splitlines()
            failures.append(f"{rel}: py_compile — {detail[-1][:200] if detail else 'no output'}")
    for rel in files[:5]:
        target = repo / rel
        if not target.exists():
            continue  # already reported above
        spec_import = (f"import sys; sys.path.insert(0, r'{repo}'); "
                       f"import {_module_of(rel)}")
        probe = subprocess.run(
            [sys.executable, "-c", spec_import],
            capture_output=True, text=True, timeout=90, cwd=str(repo),
        )
        if probe.returncode != 0:
            detail = (probe.stderr or "").strip().splitlines()
            failures.append(f"{rel}: import — {detail[-1][:200] if detail else 'no output'}")
    return {"files": files, "failures": failures, "passed": not failures}


def _reverse_apply(repo_path: Path, diff_text: str) -> bool:
    try:
        reversed_apply = run_bounded(
            ["git", "-C", str(repo_path), "apply", "-R", "--recount", "-C1",
             "--whitespace=nowarn", "-"],
            input_text=str(diff_text or ""), timeout=60,
        )
    except SandboxError as exc:
        logger.warning("repair rollback could not run: %s", exc)
        return False
    return reversed_apply.returncode == 0


def _verdicts_from_ledger(ledger: ReviewLedger, submission_id: Optional[str]) -> List[Dict[str, Any]]:
    if not submission_id:
        return []
    submission = ledger.get(submission_id)
    if submission is None:
        return []
    verdicts: List[Dict[str, Any]] = []
    for review in submission.reviews:
        evidence = dict(review.get("evidence") or {})
        verdicts.append({
            "stage": review["stage"], "agent": review["actor"],
            "verdict": review["verdict"],
            "rationale": str(evidence.get("rationale") or "")[:1000],
            "findings": [str(item)[:500] for item in (evidence.get("findings") or [])[:20]],
            "failures": [str(item)[:500] for item in (evidence.get("failures") or [])[:20]],
            "command": str(evidence.get("command") or "")[:200],
            "files_checked": [str(item)[:300] for item in (evidence.get("files_checked") or [])[:100]],
            "model": str(evidence.get("model") or "")[:200],
            "reviewed_at": review["reviewed_at"],
        })
    return verdicts


# -------------------------------------------------------------- case runner

def run_case(
    case_id: str, *,
    config: Any = None,
    services: Optional[Dict[str, Any]] = None,
    repo_path: Optional[Path] = None,
    store: Optional[RepairCaseStore] = None,
    ledger: Optional[ReviewLedger] = None,
    max_attempts: Optional[int] = None,
    worktree_parent: Optional[Path] = None,
) -> Dict[str, Any]:
    """Walk one case through the full agent chain and auto-apply on PASS."""
    store = store or RepairCaseStore()
    case = store.get(case_id)
    if case is None:
        raise WatchdogError(f"unknown case_id: {case_id}")
    if case.get("status") in TERMINAL_STATUSES:
        raise WatchdogError(f"case is {case.get('status')}; reopen detection instead")
    ledger = ledger or ReviewLedger()
    if repo_path is None:
        from core.config import BASE_DIR
        repo_path = Path(BASE_DIR)
    cfg = auto_repair_config(config)
    attempts_allowed = int(max_attempts or cfg["max_attempts"])

    if case.get("kind") == "OWNER_ACTION":
        # not a code defect: a capability only the owner can configure
        # (credential, training run, external tooling) — no coding-agent run
        store.update(case_id, status="OWNER_ACTION",
                     _note="owner action required — the coding agent cannot configure credentials")
        return store.get(case_id)

    store.update(case_id, status="IN_PROGRESS",
                 _note=f"coding agent starting (≤{attempts_allowed} attempts)")
    record_activity(ACTIVITY_WORKING, case_id=case_id,
                    title=str(case.get("title") or ""),
                    stage="starting", detail=f"chain started: {case.get('title', '')[:160]}")
    case_context = dict(case.get("context") or {})
    context_files = dict(case.get("context_files") or {})
    source_file = str(case_context.get("file") or "").strip().replace("\\", "/")
    allowed_files = case_context.get("allowed_files") or []
    if isinstance(allowed_files, str):
        allowed_files = [allowed_files]
    source_files = [
        str(path).strip().replace("\\", "/")
        for path in [source_file, *allowed_files]
        if str(path).strip()
    ]
    missing_context = [path for path in dict.fromkeys(source_files) if path not in context_files]
    if missing_context:
        context_files.update(read_context_files(Path(repo_path), missing_context))
    if not context_files:
        # Cases created without an explicit file (runtime errors, staleness
        # reports) still point at their suspect module — either by naming the
        # path outright or through incident keywords ("Pre-Earnings scanner").
        # Deriving real excerpts anchors the generated diff to actual file
        # content; without it the LLM hallucinates context lines and every
        # diff fails with "patch does not apply".
        import re as _re

        def _normalise(value: str) -> str:
            return value.lower().replace("-", "_").replace("/", "_")

        raw_text = f"{case.get('problem') or ''} {case.get('title') or ''}"
        hinted = _re.findall(r"(?:[A-Za-z0-9_.\-]+/)*[A-Za-z0-9_\-]+\.py", raw_text)
        derived: list = []
        for path in hinted:
            normalized = path.replace("\\", "/")
            if normalized not in derived and (Path(repo_path) / normalized).is_file():
                derived.append(normalized)
        if not derived:
            keywords = [
                token for token in _re.split(r"[^a-z0-9]+", _normalise(str(case.get("title") or "")))
                if len(token) >= 5
            ]
            if keywords:
                scored: dict = {}
                skip_prefixes = (".venv", "node_modules", "runtime/", "logs/", ".git", "tests/")
                for py_file in Path(repo_path).rglob("*.py"):
                    rel = py_file.relative_to(repo_path).as_posix()
                    if rel.startswith(skip_prefixes):
                        continue
                    norm = _normalise(rel)[:-3]
                    score = sum(1 for kw in keywords if kw in norm)
                    if score:
                        scored[rel] = score
                derived = sorted(scored, key=scored.get, reverse=True)[:3]
        if derived:
            context_files.update(read_context_files(Path(repo_path), derived[:4]))
    task = RepairTask(
        experiment_id=f"REPAIR-{case_id.replace('case_', '').upper()}",
        problem=str(case.get("problem") or ""),
        target_hint=str(case.get("context", {}).get("target_hint") or ""),
        context=case_context,
        context_files=context_files,
    )
    result = run_auto_repair(
        task, config=config, services=services,
        max_attempts=attempts_allowed, repo_path=repo_path,
        ledger=ledger, worktree_parent=worktree_parent,
        progress=lambda stage, detail: record_activity(
            ACTIVITY_WORKING, case_id=case_id,
            title=str(case.get("title") or ""), stage=stage, detail=detail[:300]),
    )
    outcome = result["outcome"]
    attempts = result["attempts"]
    base = {"attempts": attempts, "outcome": outcome,
            "feedback_history": result.get("feedback_history") or [],
            "role_assignments": result.get("role_assignments") or {}}

    if outcome == "NO_CODE_DEFECT":
        store.update(case_id, status="CLOSED_NO_DEFECT", **base,
                     error=None, _note="coding agent: no code-level cause")
        record_activity(ACTIVITY_IDLE, case_id=case_id, stage="done",
                        detail="no code-level cause: " + str(
                            (result.get("attempts") or [{}])[-1].get("error", ""))[:200],
                        outcome="CLOSED_NO_DEFECT")
        return store.get(case_id)

    if outcome != "ADVANCED_TO_QUANT_VALIDATION":
        store.update(case_id, status="FAILED", **base,
                     error=result.get("notes") and "; ".join(result["notes"]) or outcome,
                     _note=f"chain outcome: {outcome}")
        record_activity(ACTIVITY_IDLE, case_id=case_id, stage="done",
                        detail=f"chain outcome: {outcome}", outcome="FAILED")
        return store.get(case_id)

    final = result.get("final_submission") or {}
    submission_id = final.get("submission_id")
    worktree = final.get("worktree")
    # run_auto_repair returns it top-level (final_submission is the submission
    # store's record and does not carry worktree metadata)
    base_tree = result.get("base_tree") or final.get("base_tree")
    verdicts = _verdicts_from_ledger(ledger, submission_id)
    store.update(case_id, submission_id=submission_id, verdicts=verdicts, **base)

    # the reviewed diff lives uncommitted in the sandbox worktree; new files are
    # untracked until staged, so stage first (excluding py_compile artifacts)
    # and read the canonical diff
    diff_text = ""
    if worktree and Path(worktree).exists():
        staged = subprocess.run(
            ["git", "-C", str(worktree), "add", "-A", "--", ".",
             ":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.pyc"],
            capture_output=True, text=True, timeout=60,
        )
        if staged.returncode != 0:  # older git without exclude pathspecs
            subprocess.run(
                ["git", "-C", str(worktree), "add", "-A", "."],
                capture_output=True, text=True, timeout=60,
            )
        # Diff against the dirty-sync baseline when one was recorded: the
        # reviewed diff must contain ONLY the Coding Agent's changes. Diffing
        # against HEAD would also ship the owner's uncommitted hunks, which
        # production already contains (apply then fails or double-applies).
        diff_args = ["git", "-C", str(worktree), "diff", base_tree] if base_tree else \
            ["git", "-C", str(worktree), "diff", "--cached", "HEAD"]
        try:
            diff_text = run_bounded(diff_args, timeout=60).stdout.strip()
        except SandboxError as exc:
            logger.warning("reviewed diff could not be read from the sandbox: %s", exc)
            diff_text = ""
    if not diff_text:
        store.update(case_id, status="FAILED", error="reviewed diff unavailable from sandbox",
                     _note="worktree missing after review")
        if worktree:
            remove_worktree(Path(repo_path), task.experiment_id + "-R1")
        record_activity(ACTIVITY_IDLE, case_id=case_id, stage="done",
                        detail="reviewed diff unavailable from sandbox", outcome="FAILED")
        return store.get(case_id)
    diff_sha = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()

    if case.get("kind") == "IMPROVEMENT":
        # owner policy: improvements are PROPOSED, never auto-applied — the
        # reviewed diff waits for the human APPROVE on the card
        store.update(case_id, status="PROPOSED", diff_sha256=diff_sha, diff_text=diff_text,
                     _note="improvement reviewed by the chain; awaiting owner approval")
        remove_worktree(Path(repo_path), task.experiment_id + "-R1")
        record_activity(ACTIVITY_WAITING_OWNER, case_id=case_id, stage="proposed",
                        detail="improvement reviewed by the chain — waiting for the owner's APPROVE",
                        outcome="PROPOSED")
        return store.get(case_id)

    return _apply_reviewed_diff(case_id, store, repo_path, diff_text, diff_sha,
                                task.experiment_id)


def _detect_conflict_markers(repo_path: Path, diff_text: str) -> list:
    """Files touched by the diff that now contain 3-way conflict markers."""
    import re as _re
    files = []
    for m in _re.finditer(r"^\+\+\+ b/(\S+)", diff_text or "", _re.M):
        rel = m.group(1)
        target = Path(repo_path) / rel
        if not target.is_file():
            continue
        try:
            head = target.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            continue
        if "<<<<<<< " in head and ">>>>>>> " in head:
            files.append(rel)
    return files


def _reset_unmerged_index(repo_path: Path, files: list) -> None:
    """Clear unmerged index entries a conflicted `git apply --3way` left."""
    for rel in files:
        subprocess.run(["git", "-C", str(repo_path), "reset", "--", rel],
                       capture_output=True, text=True, timeout=60)


def _diff_paths(diff_text: str) -> List[str]:
    """Repo-relative paths a unified diff touches (old and new side)."""
    import re as _re
    paths: List[str] = []
    for m in _re.finditer(r"^(?:--- a/|\+\+\+ b/)(\S+)", diff_text or "", _re.M):
        rel = m.group(1)
        if rel not in paths:
            paths.append(rel)
    return paths


def _index_entries(repo_path: Path, rel: str) -> Optional[str]:
    """`git ls-files -s` for one path (None when git could not be asked)."""
    try:
        done = run_bounded(["git", "-C", str(repo_path), "ls-files", "-s", "--", rel], timeout=60)
    except SandboxError:
        return None
    return done.stdout if done.returncode == 0 else None


def _snapshot_touched_files(repo_path: Path, diff_text: str) -> Dict[str, Dict[str, Any]]:
    """Bytes and index entries of every file the diff touches, before the apply.

    `git apply -R` cannot undo a conflicted 3-way merge (the hunks no longer
    match a file full of markers) and never touches the index, so the only
    reliable rollback is to put back exactly what was there.
    """
    root = Path(repo_path).resolve()
    snapshot: Dict[str, Dict[str, Any]] = {}
    for rel in _diff_paths(diff_text):
        target = (root / rel).resolve()
        if root != target and root not in target.parents:
            continue                                   # never follow a path out of the repo
        try:
            content = target.read_bytes() if target.is_file() else None
        except OSError:
            continue
        snapshot[rel] = {"content": content, "index": _index_entries(root, rel)}
    return snapshot


def _restore_touched_files(repo_path: Path, snapshot: Dict[str, Dict[str, Any]]) -> bool:
    """Put the snapshot back (file bytes + index entries). True when every
    touched file is byte-identical to the snapshot afterwards."""
    root = Path(repo_path).resolve()
    restored = True
    for rel, before in (snapshot or {}).items():
        target = root / rel
        try:
            current = target.read_bytes() if target.is_file() else None
            if current != before["content"]:
                if before["content"] is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(before["content"])
        except OSError as exc:
            logger.warning("repair rollback could not restore %s: %s", rel, exc)
            restored = False
            continue
        index_before = before.get("index")
        if index_before is None or _index_entries(root, rel) == index_before:
            continue
        try:
            # drops unmerged stages and anything the apply staged ...
            run_bounded(["git", "-C", str(root), "reset", "-q", "--", rel], timeout=60)
            # ... then puts back what was staged before the apply
            for line in index_before.splitlines():
                meta = line.split("\t", 1)[0].split()
                if len(meta) == 3 and meta[2] == "0":
                    run_bounded(["git", "-C", str(root), "update-index", "--add",
                                 "--cacheinfo", f"{meta[0]},{meta[1]},{rel}"], timeout=60)
        except SandboxError as exc:
            logger.warning("repair rollback could not restore the index entry of %s: %s", rel, exc)
    return restored


def _apply_reviewed_diff(
    case_id: str, store: RepairCaseStore, repo_path: Path,
    diff_text: str, diff_sha: str, experiment_id: str,
) -> Dict[str, Any]:
    """Apply a reviewed diff to production + post-check + automatic rollback."""
    snapshot = _snapshot_touched_files(Path(repo_path), diff_text)
    try:
        # three_way: the reviewed diff is diffed against HEAD, so hunks the
        # owner's uncommitted working-tree changes already contain must merge
        # as no-ops instead of failing the production apply.
        apply_unified_diff(Path(repo_path), diff_text + "\n", three_way=True)
    except Exception as exc:
        # A conflicted 3-way merge raises here ("Applied patch ... with
        # conflicts") AFTER git wrote <<<<<<< markers into the live file and
        # unmerged entries into the index: put both back before giving up.
        restored = _restore_touched_files(Path(repo_path), snapshot)
        state = ("touched files restored to their previous content" if restored
                 else "touched files could NOT be restored - check them by hand")
        store.update(case_id, status="FAILED", error=f"apply failed: {exc} ({state})"[:900],
                     _note="production apply rejected the reviewed diff")
        remove_worktree(Path(repo_path), experiment_id + "-R1")
        record_activity(ACTIVITY_IDLE, case_id=case_id, stage="done",
                        detail=f"apply failed: {str(exc)[:200]}", outcome="FAILED")
        return store.get(case_id)

    # `git apply --3way` exits 0 even when the MERGE CONFLICTED — it leaves
    # <<<<<<< markers in the file and unmerged index entries, which blocks
    # every later `git commit` until a human resolves it. Treat conflict
    # markers in any touched file as a failed apply (owner-spec fail-safe).
    conflict_files = _detect_conflict_markers(Path(repo_path), diff_text)
    check = post_apply_check(Path(repo_path), diff_text)
    if conflict_files:
        check = {**check, "passed": False,
                 "failures": (check.get("failures") or []) + [
                     "3-way merge left conflict markers in: " + ", ".join(conflict_files)]}
    if not check["passed"]:
        rolled = _reverse_apply(Path(repo_path), diff_text + "\n")
        # `git apply -R` cannot match its hunks on a conflict-marked file and
        # never undoes what the 3-way apply staged, so the snapshot taken
        # before the apply is put back as well: file bytes and index entries.
        if conflict_files:
            _reset_unmerged_index(Path(repo_path), conflict_files)
        rolled = _restore_touched_files(Path(repo_path), snapshot) or rolled
        store.update(
            case_id, status="ROLLED_BACK" if rolled else "FAILED",
            diff_sha256=diff_sha, diff_text=diff_text,
            applied_files=check["files"], post_check=check,
            error="post-apply sanity failed: " + "; ".join(check["failures"])[:500],
            _note="automatic rollback (post-apply check)",
        )
        remove_worktree(Path(repo_path), experiment_id + "-R1")
        record_activity(ACTIVITY_IDLE, case_id=case_id, stage="rolled-back",
                        detail="post-apply check failed — change rolled back automatically",
                        outcome="ROLLED_BACK" if rolled else "FAILED")
        return store.get(case_id)

    store.update(
        case_id, status="APPLIED", diff_sha256=diff_sha, diff_text=diff_text,
        applied_files=check["files"], post_check=check,
        applied_at=_utc_now(), error=None,
        _note="approved by independent review chain; auto-applied and verified",
    )
    remove_worktree(Path(repo_path), experiment_id + "-R1")
    record_activity(ACTIVITY_IDLE, case_id=case_id, stage="done",
                    detail=f"applied {len(check['files'])} file(s) and verified",
                    outcome="APPLIED")
    logger.info("auto-repair case %s APPLIED (%s files)", case_id, len(check["files"]))
    return store.get(case_id)


def approve_case(case_id: str, *, repo_path: Optional[Path] = None,
                 store: Optional[RepairCaseStore] = None) -> Dict[str, Any]:
    """Owner approves a PROPOSED improvement: apply + post-check + rollback."""
    store = store or RepairCaseStore()
    case = store.get(case_id)
    if case is None:
        raise WatchdogError(f"unknown case_id: {case_id}")
    if case.get("status") != "PROPOSED":
        raise WatchdogError(f"case is {case.get('status')}; only PROPOSED can be approved")
    diff_text = str(case.get("diff_text") or "")
    if not diff_text:
        raise WatchdogError("proposed diff is missing from the case")
    if repo_path is None:
        from core.config import BASE_DIR
        repo_path = Path(BASE_DIR)
    diff_sha = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    store.update(case_id, status="IN_PROGRESS", _note="owner approved; applying")
    return _apply_reviewed_diff(case_id, store, Path(repo_path), diff_text, diff_sha,
                                case.get("case_id", case_id).upper())


def process_pending(store: Optional[RepairCaseStore] = None, *, limit: int = MAX_CASES_PER_CYCLE,
                    config: Any = None, services: Optional[Dict[str, Any]] = None,
                    repo_path: Optional[Path] = None, ledger: Optional[ReviewLedger] = None) -> List[Dict[str, Any]]:
    store = store or RepairCaseStore()
    processed: List[Dict[str, Any]] = []
    # OWNER_ACTION cases are free (no LLM chain) — resolve all of them first
    # so owner-facing gaps never wait behind the per-cycle repair budget.
    for case in store.list_cases(limit=50, status="DETECTED", order="ASC"):
        if case.get("kind") != "OWNER_ACTION":
            continue
        try:
            processed.append(run_case(case["case_id"], config=config, services=services,
                                      repo_path=repo_path, store=store, ledger=ledger))
        except Exception as exc:
            logger.exception("owner-action case %s crashed", case.get("case_id"))
            try:
                store.update(case["case_id"], status="FAILED", error=str(exc)[:500],
                             _note="runner crash")
            except WatchdogError:
                pass
    for case in store.list_cases(limit=limit, status="DETECTED", order="ASC"):
        if case.get("kind") == "OWNER_ACTION":
            continue
        fresh = store.get(case.get("case_id", ""))
        if not fresh or fresh.get("status") != "DETECTED":
            continue
        try:
            processed.append(run_case(case["case_id"], config=config, services=services,
                                      repo_path=repo_path, store=store, ledger=ledger))
        except WatchdogError as exc:
            logger.debug("repair case %s skipped: %s", case.get("case_id"), exc)
        except Exception as exc:  # fail-open: one bad case never stops the watchdog
            logger.exception("repair case %s crashed", case.get("case_id"))
            try:
                store.update(case["case_id"], status="FAILED", error=str(exc)[:500],
                             _note="runner crash")
            except WatchdogError:
                pass
    return processed


# ------------------------------------------------- cases cut off by a restart

MAX_INTERRUPTED_REQUEUES = 2
_INTERRUPTED_SWEEP_DONE = threading.Event()


def requeue_interrupted_cases(store: Optional[RepairCaseStore] = None, *,
                              activity_path: Optional[Path] = None) -> List[str]:
    """Queue again the cases a process exit left IN_PROGRESS.

    The repair chain runs inside this process, so right after start no case can
    really be in progress. Left alone such a case stays open forever and its
    fingerprint blocks every new detection of the same problem. A case cut off
    more than MAX_INTERRUPTED_REQUEUES times is closed as FAILED instead.
    """
    store = store or RepairCaseStore()
    requeued: List[str] = []
    for case in store.list_cases(limit=200, status="IN_PROGRESS", order="ASC"):
        case_id = str(case.get("case_id") or "")
        if not case_id:
            continue
        count = int(case.get("interrupted_count") or 0) + 1
        try:
            if count > MAX_INTERRUPTED_REQUEUES:
                store.update(case_id, status="FAILED", interrupted_count=count,
                             error=f"repair interrupted {count} times by a restart or a hang",
                             _note="interrupted again; not queued any more")
            else:
                store.update(case_id, status="DETECTED", interrupted_count=count,
                             _note="interrupted by a restart; queued again")
                requeued.append(case_id)
        except WatchdogError:
            continue
    try:
        if activity_status(activity_path).get("state") == ACTIVITY_WORKING:
            record_activity(ACTIVITY_IDLE, stage="done", path=activity_path,
                            detail="the previous run was interrupted by a restart")
    except Exception:
        logger.debug("repair activity reset skipped", exc_info=True)
    return requeued


# ------------------------------------------------------------ the watchdog

def watchdog_config(config: Any) -> Dict[str, Any]:
    base = auto_repair_config(config)
    root = getattr(config, "config", config) if config is not None else {}
    section = ((root or {}).get("development_factory") or {}).get("auto_repair") if isinstance(root, dict) else None
    section = section if isinstance(section, dict) else {}
    try:
        interval = int(section.get("watchdog_interval_seconds", DEFAULT_WATCHDOG_INTERVAL) or DEFAULT_WATCHDOG_INTERVAL)
    except (TypeError, ValueError):
        interval = DEFAULT_WATCHDOG_INTERVAL
    try:
        per_cycle = int(section.get("max_cases_per_cycle", MAX_CASES_PER_CYCLE) or MAX_CASES_PER_CYCLE)
    except (TypeError, ValueError):
        per_cycle = MAX_CASES_PER_CYCLE
    try:
        cooldown_h = float(section.get("improvement_cooldown_hours", IMPROVEMENT_COOLDOWN_HOURS)
                           or IMPROVEMENT_COOLDOWN_HOURS)
    except (TypeError, ValueError):
        cooldown_h = IMPROVEMENT_COOLDOWN_HOURS
    base.update({
        "watchdog_enabled": bool(section.get("watchdog_enabled", False)),
        "watchdog_interval_seconds": max(MIN_WATCHDOG_INTERVAL, interval),
        "max_cases_per_cycle": max(1, min(3, per_cycle)),
        "improvements_enabled": bool(section.get("improvements_enabled", False)),
        "improvement_cooldown_hours": max(1.0, cooldown_h),
    })
    return base


_IMPROVEMENT_AREAS = (
    ("strategy evidence", "study the strategy research/performance caches and the "
     "core/strategy_v2/ evaluation pipeline"),
    ("backend reliability", "study the api/ services and core/ read models the cockpit "
     "renders — latency, error handling, cache behaviour"),
    ("code quality", "study core/ modules with the most complex logic for clarity, "
     "dead code and safer data handling"),
)


def maybe_scout_improvement(store: RepairCaseStore, cfg: Dict[str, Any]) -> Optional[str]:
    """When everything is healthy, PROPOSE one bounded improvement (owner-gated
    result: the reviewed diff waits as PROPOSED, never auto-applied)."""
    if not cfg.get("improvements_enabled"):
        return None
    cooldown_h = float(cfg.get("improvement_cooldown_hours") or IMPROVEMENT_COOLDOWN_HOURS)
    for case in store.list_cases(limit=100):
        if case.get("kind") != "IMPROVEMENT":
            continue
        if case.get("status") in OPEN_STATUSES + ("PROPOSED",):
            return None  # one live proposal at a time
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(case.get("closed_at") or case.get("updated_at"))).total_seconds() / 3600
        except ValueError:
            age_h = 0.0
        if age_h < cooldown_h:
            return None
    area_index = int(time.time() // 3600) % len(_IMPROVEMENT_AREAS)
    area_name, area_hint = _IMPROVEMENT_AREAS[area_index]
    case = store.enqueue(CaseDraft(
        fingerprint=_fingerprint("improvement", area_name,
                                 time.strftime("%Y%m%d-%H")),
        kind="IMPROVEMENT", source_agent="research_scientist",
        title=f"Improvement proposal: {area_name}",
        problem=(
            f"IMPROVEMENT SCOUT (the system is currently healthy — no defects to "
            f"repair). Propose exactly ONE bounded, low-risk improvement for: "
            f"{area_name}. To ground it, {area_hint}. The improvement must NOT "
            f"touch protected paths, must NOT increase risk, sizing, autonomy or "
            f"weaken costs/gates/golden tests, and must stay a minimal diff. "
            f"Output the implementing diff; if you find nothing genuinely worth "
            f"changing, answer NO_CODE_DEFECT."
        ),
        context={"area": area_name, "scout": True},
    ))
    return case["case_id"] if case else None


def run_watchdog_cycle(*, config: Any = None, services: Optional[Dict[str, Any]] = None,
                       store: Optional[RepairCaseStore] = None,
                       repo_path: Optional[Path] = None) -> Dict[str, Any]:
    """One bounded detection + processing pass (scheduler tick or manual run)."""
    store = store or RepairCaseStore()
    cfg = watchdog_config(config)
    payload = _fetch_cockpit_payload()
    detection = run_detection(store, config)
    resolved_actions = resolve_owner_actions(store, payload)
    limit = cfg["max_cases_per_cycle"]
    processed = process_pending(store, limit=limit, config=config,
                                services=services, repo_path=repo_path)
    improvement = maybe_scout_improvement(store, cfg)
    if improvement:
        # the scout proposal runs through the same chain immediately (bounded)
        try:
            run_case(improvement, config=config, services=services,
                     repo_path=repo_path, store=store)
        except Exception:
            logger.exception("improvement scout chain failed")
    return {
        "detection": detection,
        "resolved_owner_actions": resolved_actions,
        "processed": [
            {"case_id": c.get("case_id"), "status": c.get("status"),
             "outcome": c.get("outcome")} for c in processed
        ],
        "improvement_scouted": improvement,
    }


class RepairWatchdog:
    """Daemon thread: detect problems and drive the repair chain non-stop."""

    def __init__(self, *, interval_seconds: int = DEFAULT_WATCHDOG_INTERVAL,
                 max_cases_per_cycle: int = MAX_CASES_PER_CYCLE,
                 startup_delay: int = 90, config: Any = None):
        self.interval_seconds = max(MIN_WATCHDOG_INTERVAL, int(interval_seconds or DEFAULT_WATCHDOG_INTERVAL))
        self.max_cases_per_cycle = max(1, min(3, int(max_cases_per_cycle or MAX_CASES_PER_CYCLE)))
        self.startup_delay = max(0, int(startup_delay))
        self.config = config
        self._stop = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._status_lock = threading.Lock()
        self._status: Dict[str, Any] = {
            "running": False, "enabled": False,
            "interval_seconds": self.interval_seconds,
            "cycles_completed": 0, "cases_applied": 0, "cases_failed": 0,
            "last_run": None, "last_result": None, "last_error": "",
            "cycle_started_at": None, "last_heartbeat": None,
        }

    def status(self) -> Dict[str, Any]:
        with self._status_lock:
            payload = dict(self._status)
        thread_alive = bool(self._thread is not None and self._thread.is_alive())
        cycle_started = payload.get("cycle_started_at")
        cycle_age = None
        if cycle_started:
            try:
                parsed = datetime.fromisoformat(str(cycle_started).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                cycle_age = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
            except (TypeError, ValueError):
                cycle_age = None
        stale_after = max(600.0, float(self.interval_seconds) * 2.0)
        stalled = bool(cycle_age is not None and cycle_age > stale_after)
        if stalled:
            # A long repair cycle (LLM coding/review per case, max_cases
            # bounded but not time-bounded) legitimately exceeds the interval;
            # the loop is only truly stalled if the per-stage activity
            # heartbeat went quiet as well.
            heartbeat_age: Optional[float] = None
            try:
                activity = activity_status()
                updated = str((activity or {}).get("updated_at") or "").strip()
                if updated:
                    parsed_activity = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    if parsed_activity.tzinfo is None:
                        parsed_activity = parsed_activity.replace(tzinfo=timezone.utc)
                    heartbeat_age = max(
                        0.0, (datetime.now(timezone.utc) - parsed_activity).total_seconds()
                    )
            except Exception:
                heartbeat_age = None
            if heartbeat_age is not None and heartbeat_age <= stale_after:
                stalled = False
        payload.update({
            "thread_alive": thread_alive,
            "cycle_age_seconds": round(cycle_age, 1) if cycle_age is not None else None,
            "stale_after_seconds": stale_after,
            "stalled": stalled,
            "running": bool(payload.get("enabled") and thread_alive and not stalled),
        })
        if stalled and not payload.get("last_error"):
            payload["last_error"] = f"watchdog cycle stalled for {cycle_age:.0f}s"
        return payload

    def _record(self, **fields: Any) -> None:
        with self._status_lock:
            self._status.update(fields)

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="repair-watchdog", daemon=True)
        self._thread.start()
        self._record(running=True, enabled=True)
        logger.info("Repair watchdog started (interval=%ss)", self.interval_seconds)
        return True

    def stop(self) -> bool:
        was = self.status()["running"]
        self._stop.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._thread = None
        self._record(running=False, enabled=False)
        return bool(was)

    def request_run(self) -> None:
        """Wake the daemon for an immediate bounded cycle."""
        self._wake_event.set()

    def run_once_now(self) -> Dict[str, Any]:
        self._record(cycle_started_at=_utc_now(), last_heartbeat=_utc_now())
        try:
            result = run_watchdog_cycle(config=self.config)
            self._record(cycles_completed=self.status()["cycles_completed"] + 1,
                         last_run=_utc_now(), last_result=result, last_error="",
                         last_heartbeat=_utc_now())
            for entry in result["processed"]:
                if entry.get("status") == "APPLIED":
                    self._record(cases_applied=self.status()["cases_applied"] + 1)
                elif entry.get("status") in ("FAILED", "ROLLED_BACK"):
                    self._record(cases_failed=self.status()["cases_failed"] + 1)
            return result
        except Exception as exc:
            self._record(last_error=str(exc)[:500])
            raise
        finally:
            self._record(cycle_started_at=None, last_heartbeat=_utc_now())

    def _loop(self) -> None:
        if not _INTERRUPTED_SWEEP_DONE.is_set():  # once per process
            _INTERRUPTED_SWEEP_DONE.set()
            try:
                requeued = requeue_interrupted_cases()
                if requeued:
                    logger.info("Repair watchdog queued again %d interrupted case(s)", len(requeued))
            except Exception:
                logger.exception("repair watchdog could not requeue interrupted cases")
        if self.startup_delay:
            self._wake_event.wait(self.startup_delay)
            self._wake_event.clear()
        while not self._stop.is_set():
            try:
                self.run_once_now()
            except Exception as exc:  # fail-open: the watchdog survives crashes
                logger.exception("repair watchdog cycle failed")
                self._record(last_error=str(exc)[:500])
            self._wake_event.wait(self.interval_seconds)
            self._wake_event.clear()


_WATCHDOG_LOCK = threading.Lock()
_WATCHDOG: Optional[RepairWatchdog] = None


def get_repair_watchdog(config: Any = None) -> RepairWatchdog:
    global _WATCHDOG
    with _WATCHDOG_LOCK:
        if _WATCHDOG is None:
            cfg = watchdog_config(config)
            _WATCHDOG = RepairWatchdog(
                interval_seconds=cfg["watchdog_interval_seconds"],
                max_cases_per_cycle=cfg["max_cases_per_cycle"],
                config=config,
            )
        return _WATCHDOG


def shutdown_repair_watchdog() -> bool:
    global _WATCHDOG
    with _WATCHDOG_LOCK:
        if _WATCHDOG is None:
            return False
        stopped = _WATCHDOG.stop()
        _WATCHDOG = None
        return stopped


def watchdog_startup(config: Any = None) -> Dict[str, Any]:
    """Lifespan hook: start the daemon when the owner enabled it."""
    # The watchdog patches source files of the running install. A developer
    # working on the same tree needs a way to run the server without it.
    if os.environ.get("MS_DISABLE_REPAIR_WATCHDOG", "").strip() == "1":
        return {"started": False, "enabled": False, "reason": "MS_DISABLE_REPAIR_WATCHDOG=1"}
    cfg = watchdog_config(config)
    root = getattr(config, "config", config) if config is not None else {}
    agents = (root or {}).get("agents", {}) if isinstance(root, dict) else {}
    coding_enabled = bool((agents.get("coding_agent") or {}).get("enabled", True))
    critic_enabled = bool((agents.get("critic_agent") or {}).get("enabled", True))
    if not (cfg["watchdog_enabled"] and cfg["enabled"] and coding_enabled and critic_enabled):
        return {"started": False, "enabled": False}
    get_repair_watchdog(config).start()
    return {"started": True, "enabled": True}


# ------------------------------------------------------------- UI summaries

_ATTEMPT_STAGE_BY_STATUS = {
    "GENERATION_FAILED": "CODING",
    "NO_DEFECT": "CODING",
    "SCOPE_REJECTED": "SCOPE_GUARD",
    "PIPELINE_REJECTED": "POLICY_OR_SANDBOX",
}


def _case_failure_stage(case: Dict[str, Any]) -> str:
    """Return the last evidenced stage, without pretending an unrun gate ran."""
    status = str(case.get("status") or "").upper()
    error = str(case.get("error") or "").lower()
    if "post-apply" in error or status == "ROLLED_BACK":
        return "POST_APPLY_CHECK"
    if error.startswith("apply failed"):
        return "PRODUCTION_APPLY"
    if "reviewed diff unavailable" in error:
        return "REVIEWED_DIFF_CAPTURE"
    attempts = case.get("attempts") or []
    if attempts:
        last = attempts[-1] if isinstance(attempts[-1], dict) else {}
        events = last.get("events") or []
        failed_events = [
            event for event in events
            if isinstance(event, dict)
            and str(event.get("status") or "").upper() in {
                "FAILED", "NOT_PERFORMED", "NO_DEFECT",
            }
        ]
        if failed_events:
            return str(failed_events[-1].get("stage") or "UNKNOWN")
        stages = last.get("stages") or {}
        if isinstance(stages, dict):
            blocked = [stage for stage, verdict in stages.items()
                       if str(verdict).upper() != "PASSED"]
            if blocked:
                return str(blocked[-1])
        attempt_status = str(last.get("status") or "").upper()
        if attempt_status in _ATTEMPT_STAGE_BY_STATUS:
            return _ATTEMPT_STAGE_BY_STATUS[attempt_status]
    outcome = str(case.get("outcome") or "").upper()
    if outcome in {"NO_PROVIDER", "GENERATION_UNAVAILABLE"}:
        return "AI_PROVIDER"
    if outcome == "REVIEW_UNAVAILABLE":
        return "INDEPENDENT_REVIEW"
    if status == "OWNER_ACTION":
        return "OWNER_CONFIGURATION"
    if status == "DETECTED":
        return "DETECTION"
    if status == "IN_PROGRESS":
        return "IN_PROGRESS"
    if status == "APPLIED":
        return "COMPLETE"
    return "CASE_LIFECYCLE"


def _case_reason(case: Dict[str, Any]) -> str:
    """Select the most specific persisted explanation for the UI."""
    case_error = str(case.get("error") or "").strip()
    generic_labels = {
        str(case.get("outcome") or "").strip().upper(),
        str(case.get("status") or "").strip().upper(),
    }
    if case_error and case_error.upper() not in generic_labels:
        return case_error
    attempts = case.get("attempts") or []
    if attempts and isinstance(attempts[-1], dict) and attempts[-1].get("error"):
        return str(attempts[-1]["error"])
    if case_error:
        return case_error
    feedback = case.get("feedback_history") or []
    if feedback:
        return str(feedback[-1])
    history = case.get("history") or []
    if history and isinstance(history[-1], dict) and history[-1].get("note"):
        return str(history[-1]["note"])
    return str(case.get("outcome") or case.get("status") or "No diagnostic was captured.")


def _case_next_action(case: Dict[str, Any]) -> str:
    """Explain the deterministic next path for the case's persisted outcome."""
    status = str(case.get("status") or "").upper()
    outcome = str(case.get("outcome") or "").upper()
    if status == "APPLIED":
        return "No repair action is pending; the change passed post-apply verification."
    if status == "PROPOSED":
        return "Review the diff preview and approve it before production apply."
    if status == "OWNER_ACTION":
        return "This is an external configuration or credential task and cannot be changed by the Coding Agent."
    if outcome in {"NO_PROVIDER", "GENERATION_UNAVAILABLE"}:
        return "Restore an available Coding Agent provider/model or quota, then retry this case."
    if outcome == "REVIEW_UNAVAILABLE":
        return "Restore the independent reviewer provider/model, then retry; the gate remains fail-closed."
    if outcome == "NO_CODE_DEFECT" or status == "CLOSED_NO_DEFECT":
        return "No patch was proposed. Improve the detector's source evidence or resolve the external/data cause before retrying."
    if status in {"FAILED", "ROLLED_BACK"}:
        return "Inspect the failed step below, then retry; all previous feedback is retained for the next run."
    if status == "DETECTED":
        return "Run the case through Coding Agent."
    if status == "IN_PROGRESS":
        return "The chain is still running; wait for its terminal verdict."
    return "No automatic next action is recorded for this state."


def _case_target_files(case: Dict[str, Any]) -> List[str]:
    context = case.get("context") or {}
    values: List[Any] = [context.get("file"), context.get("target_hint")]
    allowed = context.get("allowed_files") or []
    values.extend([allowed] if isinstance(allowed, str) else allowed)
    values.extend(case.get("applied_files") or [])
    values.extend(_diff_files(str(case.get("diff_text") or "")))
    result: List[str] = []
    for value in values:
        path = str(value or "").strip().replace("\\", "/")
        if path and path not in result:
            result.append(path)
    return result


def _case_diagnostic(case: Dict[str, Any]) -> Dict[str, Any]:
    attempts = case.get("attempts") or []
    history = case.get("history") or []
    status = str(case.get("status") or "").upper()
    missing: List[str] = []
    if not history:
        missing.append("lifecycle history")
    if status not in {"DETECTED", "OWNER_ACTION", "RESOLVED"} and not attempts:
        missing.append("attempt trace")
    if status in {"FAILED", "ROLLED_BACK"} and not (
        case.get("error") or any(isinstance(a, dict) and a.get("error") for a in attempts)
    ):
        missing.append("failure reason")
    return {
        "reason": _case_reason(case),
        "failed_stage": _case_failure_stage(case),
        "next_action": _case_next_action(case),
        "evidence_complete": not missing,
        "missing_evidence": missing,
        "legacy_record": any(not isinstance(a, dict) or not a.get("events") for a in attempts),
        "retryable": status not in {"IN_PROGRESS", "OWNER_ACTION", "RESOLVED"},
    }

_STATE_LABELS = {
    ACTIVITY_WORKING: "Working",
    ACTIVITY_IDLE: "Idle — watching",
    ACTIVITY_WAITING_OWNER: "Waiting your approval",
}


def repair_agent_view(store: Optional[RepairCaseStore] = None, *,
                      limit: int = 20) -> Dict[str, Any]:
    """Everything the Coding Agent tab renders: live state, full per-case
    journey (who recommended what, what the agent thought at every attempt,
    every reviewer rationale), proposals and counters."""
    from collections import Counter

    store = store or RepairCaseStore()
    activity = activity_status()
    # One read of the store (it is a multi-MB JSON file): the newest `limit`
    # cases are rendered in full, while proposals and owner actions are taken
    # from the whole retained window. Building them from the 20 displayed
    # cases only made the tab say "Proposals waiting: 1" above an empty
    # proposal list (the proposal could then never be approved), and counted
    # the same owner TODO once per re-detection ("Owner actions: 60").
    all_cases = store.list_cases(limit=200)
    cases = all_cases[:max(1, int(limit or 20))]
    counts = Counter(str(c.get("status")) for c in all_cases)
    detailed: List[Dict[str, Any]] = []
    proposals: List[Dict[str, Any]] = []
    owner_actions: List[Dict[str, Any]] = []
    seen_owner_tiles: set = set()

    def build_row(case: Dict[str, Any]) -> Dict[str, Any]:
        row = {
            "case_id": case.get("case_id"),
            "kind": case.get("kind"),
            "title": case.get("title"),
            "recommended_by": case.get("source_agent"),
            "detected_at": case.get("detected_at"),
            "updated_at": case.get("updated_at"),
            "status": case.get("status"),
            "outcome": case.get("outcome"),
            "problem": case.get("problem"),          # the full recommendation
            "evidence": (case.get("context") or {}).get("evidence") or {},
            "target_files": _case_target_files(case),
            "role_assignments": case.get("role_assignments") or {},
            "attempts": [
                {"attempt": a.get("attempt") or index + 1,
                 "status": a.get("status"), "error": a.get("error"),
                 "started_at": a.get("started_at"), "finished_at": a.get("finished_at"),
                 "provider": a.get("provider"), "model": a.get("model"),
                 "submission_id": a.get("submission_id"),
                 "diff_sha256": a.get("diff_sha256"),
                 "worktree_removed": bool(a.get("worktree_removed")),
                 "stages": a.get("stages") or {},
                 "events": a.get("events") or []}
                for index, a in enumerate(case.get("attempts") or [])
                if isinstance(a, dict)
            ],
            "agent_reasoning": list(case.get("feedback_history") or []),
            "review_chain": case.get("verdicts") or [],
            "history": [
                {"at": event.get("at"), "from": event.get("from"),
                 "to": event.get("to"), "note": event.get("note")}
                for event in (case.get("history") or [])
                if isinstance(event, dict)
            ],
            "applied_files": case.get("applied_files") or [],
            "post_check": case.get("post_check"),
            "applied_at": case.get("applied_at"),
            "error": case.get("error"),
            "diagnostic": _case_diagnostic(case),
        }
        diff_text = str(case.get("diff_text") or "")
        if diff_text:
            row["diff_files"] = _diff_files(diff_text)
            row["diff_preview"] = "\n".join(diff_text.splitlines()[:40])
        return row

    for case in cases:
        detailed.append(build_row(case))
    for case in all_cases:  # newest first
        status = case.get("status")
        if status == "OWNER_ACTION":
            tile = str((case.get("context") or {}).get("tile") or case.get("title") or "").strip().lower()
            if tile in seen_owner_tiles:
                continue  # the same owner TODO, detected again later
            seen_owner_tiles.add(tile)
            row = build_row(case)
            owner_actions.append({k: row[k] for k in (
                "case_id", "title", "recommended_by", "detected_at",
                "problem", "status")})
        elif status == "PROPOSED":
            row = build_row(case)
            proposals.append({k: row.get(k) for k in (
                "case_id", "kind", "title", "recommended_by", "detected_at",
                "review_chain", "agent_reasoning", "diff_files", "diff_preview",
                "problem")} | {"status": "PROPOSED"})
    state = activity.get("state")
    return {
        "activity": {
            **activity,
            "state_label": _STATE_LABELS.get(str(state), str(state)),
        },
        "summary": {
            "total_cases": sum(counts.values()),
            "applied": counts.get("APPLIED", 0),
            "failed": counts.get("FAILED", 0),
            "rolled_back": counts.get("ROLLED_BACK", 0),
            "no_defect": counts.get("CLOSED_NO_DEFECT", 0),
            "dismissed": counts.get("DISMISSED", 0),
            "proposals_waiting": len(proposals),
            # distinct open owner TODOs (the raw case count repeats each tile)
            "owner_actions": len(owner_actions),
            "owner_action_cases": counts.get("OWNER_ACTION", 0),
            "owner_actions_resolved": counts.get("RESOLVED", 0),
            "open": counts.get("DETECTED", 0) + counts.get("IN_PROGRESS", 0),
        },
        "proposals": proposals,
        "owner_actions": owner_actions,
        "cases": detailed,
        "policies": (
            "repairs auto-apply after independent reviews pass; improvements wait "
            "for the owner's APPROVE; protected paths (risk/autonomy/costs/golden) "
            "are rejected fail-closed; a reviewer that cannot run is recorded "
            "NOT_PERFORMED, never a fake pass (§81/§112)"
        ),
    }


def case_summary_rows(store: Optional[RepairCaseStore] = None, *, limit: int = 12) -> List[Dict[str, Any]]:
    """Compact per-case rows for the cockpit card (full journey visible)."""
    store = store or RepairCaseStore()
    rows = []
    for case in store.list_cases(limit=limit):
        verdicts = case.get("verdicts") or []
        rows.append({
            "case_id": case.get("case_id"),
            "kind": case.get("kind"),
            "title": case.get("title"),
            "source_agent": case.get("source_agent"),
            "status": case.get("status"),
            "coding_attempts": len(case.get("attempts") or []),
            "coding_outcome": case.get("outcome"),
            "review_chain": [
                {"agent": v.get("agent"), "stage": v.get("stage"), "verdict": v.get("verdict")}
                for v in verdicts
            ],
            "applied_files": case.get("applied_files") or [],
            "applied_at": case.get("applied_at"),
            "post_check_passed": bool((case.get("post_check") or {}).get("passed"))
            if case.get("post_check") else None,
            "error": case.get("error"),
            "detected_at": case.get("detected_at"),
        })
    return rows


__all__ = [
    "WatchdogError", "CaseDraft", "RepairCaseStore", "detect_backtest_anomalies",
    "detect_runtime_errors", "detect_drift_cases", "detect_cockpit_section_errors",
    "detect_static_code_issues", "detect_ui_asset_issues", "detect_owner_actions", "resolve_owner_actions",
    "run_detection",
    "run_case", "approve_case", "process_pending", "maybe_scout_improvement",
    "run_watchdog_cycle", "RepairWatchdog", "get_repair_watchdog",
    "shutdown_repair_watchdog", "watchdog_startup", "watchdog_config",
    "case_summary_rows", "post_apply_check", "default_cases_path",
    "record_activity", "activity_status", "repair_agent_view",
]
