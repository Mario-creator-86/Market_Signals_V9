"""UI & Data Health Inspector Agent.

Continuously monitors all frontend tabs, API endpoints, and data pipelines for:
- Stale / frozen data (e.g. news not refreshing, radar empty, scanner stalled)
- Missing validation evidence (e.g. Strategy Lab with UNAVAILABLE OOS/robustness)
- API endpoint errors, serialization bugs, and data format regressions

When an anomaly or stall is detected, it automatically constructs an AST/code-level
incident report and escalates directly to the Coding Agent & Critic Watchdog
(RepairCaseStore) for autonomous root-cause investigation and self-repair.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.agent_toggles import is_agent_enabled
from core.observability import update_agent_status

logger = logging.getLogger("UIInspectorAgent")

_inspector_instance: Optional["UIInspectorAgent"] = None
_inspector_lock = threading.RLock()

_AGENT_REPAIR_FILES: Dict[str, List[str]] = {
    "execution": ["agents/execution_agent.py", "core/agent_manager.py"],
    "trade_radar": ["agents/trade_radar_agent.py"],
    "sentiment": ["agents/sentiment_agent.py"],
    "pre_earnings": ["agents/pre_earnings_agent.py", "core/pre_earnings/continuous.py"],
    "ui_inspector": ["agents/ui_inspector_agent.py"],
    "news_tab": ["agents/news_tab_agent.py"],
    "economic_calendar": ["agents/economic_calendar_agent.py"],
    "premarket_scanner": ["agents/premarket_scanner_agent.py"],
    "strategy_research": ["agents/strategy_research_agent.py"],
    "research_factory": [
        "core/research_factory/scheduler.py",
        "core/research_factory/pipeline.py",
        "core/research_factory/tracking.py",
        "api/research_factory_service.py",
    ],
    "scanner": ["agents/scanner_agent.py", "core/agent_manager.py"],
    "coding_agent": [
        "core/development_factory/auto_repair.py",
        "core/development_factory/watchdog.py",
    ],
    "risk": ["agents/risk_agent.py"],
    "critic_agent": [
        "core/development_factory/auto_repair.py",
        "core/development_factory/watchdog.py",
    ],
}


def _agent_incident_context(name: str, details: Dict[str, Any]) -> Dict[str, Any]:
    files = list(_AGENT_REPAIR_FILES.get(name, []))
    context: Dict[str, Any] = {
        "agent": name,
        "lifecycle_state": details.get("lifecycle_state"),
        "last_error": str(details.get("last_error") or "")[:1000],
    }
    if files:
        context["file"] = files[0]
        context["allowed_files"] = files
    return context


# Freshness budgets. A tab is only "stale" once its newest payload timestamp
# exceeds its own refresh cadence by a wide margin, so a single slow cycle or a
# restart never fires a false incident. Tabs without a machine-readable cadence
# (news, radar) use a fixed budget.
_STALE_MIN_SECONDS = 600.0
_STALE_REFRESH_MULTIPLIER = 3.0
_NEWS_RADAR_STALE_SECONDS = 900.0


def _iso_age_seconds(value: Any, now: Optional[float] = None) -> Optional[float]:
    """Age in seconds of an ISO-8601 timestamp; None when absent/unparsable."""
    raw = str(value if value is not None else "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now if now is not None else time.time()) - parsed.timestamp())


def _staleness_threshold(refresh_seconds: Any) -> float:
    try:
        refresh = float(refresh_seconds)
    except (TypeError, ValueError):
        refresh = 0.0
    return max(_STALE_MIN_SECONDS, _STALE_REFRESH_MULTIPLIER * refresh)


def _fetch_with_429_retry(fetch: Any, url: str, timeout: float, attempts: int = 3) -> Any:
    """Fetch via `fetch`, retrying HTTP 429 with Retry-After backoff; re-raises the final error."""
    for attempt in range(attempts):
        try:
            return fetch(url, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt >= attempts - 1:
                raise
            try:
                delay = float((exc.headers or {}).get("Retry-After") or 0)
            except (TypeError, ValueError):
                delay = 0.0
            logger.warning("UIInspector: %s rate-limited (429), retry %d/%d", url, attempt + 1, attempts - 1)
            time.sleep(min(10.0, max(1.0, delay)))
    return None


def get_ui_inspector_agent(config: Optional[Dict[str, Any]] = None) -> "UIInspectorAgent":
    """Factory function for singleton access."""
    global _inspector_instance
    with _inspector_lock:
        if _inspector_instance is None:
            _inspector_instance = UIInspectorAgent(config)
    return _inspector_instance


def get_ui_inspector_agent_if_ready() -> Optional["UIInspectorAgent"]:
    """Return the singleton only when already initialized."""
    return _inspector_instance


class UIInspectorAgent:
    """Monitors tab-by-tab UI data and escalates defects to Coding Agent."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self._started_at_epoch = time.time()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        # Browser-side layout audit dedup: signature -> last-accepted epoch.
        # Keeps the same overflow defect from reopening a case every 2 minutes.
        self._layout_signatures: Dict[str, float] = {}
        self._last_report: Dict[str, Any] = {
            "status": "idle",
            "last_scan_at": None,
            "tabs_checked": 0,
            "anomalies_detected": 0,
            "escalations_sent": 0,
            "tab_health": {},
        }

    @property
    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="UIInspectorAgent"
            )
            self._thread.start()
        logger.info("UIInspectorAgent started (background tab inspection loop).")

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None
        logger.info("UIInspectorAgent stopped.")

    def request_scan(self) -> Dict[str, Any]:
        """Trigger an immediate scan cycle."""
        self._wake_event.set()
        return {"status": "accepted", "message": "UI Inspector scan triggered."}

    def _wait_for_server_readiness(self, timeout: float = 120.0) -> None:
        """Hold the first scan until the API is actually accepting requests.

        The inspector runs inside the API process and inspects over HTTP, but
        during uvicorn startup the port is not accepting yet. A premature
        first scan escalates connection-refused ENDPOINT_ERRORs that the
        Coding Agent can only close as NO_DEFECT (dozens of such cases in
        the repair store).
        """
        port = os.environ.get("MS_PORT", "8003")
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.time() + max(10.0, timeout)
        while time.time() < deadline and not self._stop_event.is_set():
            try:
                self._fetch_json(f"{base_url}/api/agents", timeout=2.0)
                return
            except Exception:
                self._wake_event.wait(timeout=2.0)
                self._wake_event.clear()

    def _loop(self) -> None:
        self._wait_for_server_readiness()
        while not self._stop_event.is_set():
            try:
                cfg = (self.config.get("agents", {}) or {}).get("ui_inspector", {}) or {}
                if is_agent_enabled("ui_inspector", config=self.config, default=True):
                    self.run_inspection_cycle()
            except Exception as exc:
                logger.error(f"UIInspectorAgent cycle error: {exc}", exc_info=True)

            cfg = (self.config.get("agents", {}) or {}).get("ui_inspector", {}) or {}
            interval = max(60, int(cfg.get("interval_sec", 300) or 300))
            self._wake_event.wait(timeout=interval)
            self._wake_event.clear()

    def run_inspection_cycle(self) -> Dict[str, Any]:
        """Inspect all UI tabs and escalate any detected anomalies to Coding Agent."""
        started_at = time.perf_counter()
        now_iso = datetime.now(timezone.utc).isoformat()
        port = os.environ.get("MS_PORT", "8003")
        base_url = f"http://127.0.0.1:{port}"

        tab_health: Dict[str, Any] = {}
        anomalies: List[Dict[str, Any]] = []
        self_healed: List[Dict[str, Any]] = []

        def endpoint_error(tab: str, key: str, url: str, exc: Exception) -> None:
            message = str(exc)[:200]
            tab_health[key] = {"status": "ERROR", "error": message}
            anomalies.append({
                "tab": tab,
                "kind": "ENDPOINT_ERROR",
                "title": f"{tab} data endpoint failed",
                "problem": (
                    f"UI Inspector: {url} failed with {type(exc).__name__}: {message}. "
                    "Investigate the endpoint, its read model, and serialization path; "
                    "restore a healthy response without hiding the underlying error."
                ),
                "context": {"endpoint": url, "error": message},
            })

        # 0. Agent Fleet control plane — fetched once up front so tab
        # inspections can consult the operator's true enablement flag: the
        # `disabled` flag on tab payloads also covers market-session pauses
        # (premarket scanner idles while the market is open), which must not
        # be reported as a disabled agent.
        control_plane: Dict[str, Any] = {}
        try:
            # The control plane aggregates every agent's status payload and can
            # legitimately exceed the generic 10s budget during warm starts; give
            # it the same 30s allowance as /api/cockpit below. The timeout kwarg
            # is part of _fetch_json's signature: it is already used for
            # /api/agents in _wait_for_server_readiness and /api/cockpit herein.
            control_plane = _fetch_with_429_retry(
                self._fetch_json, f"{base_url}/api/agents/control-plane", 30.0
            )
        except Exception as exc:
            endpoint_error("Agents", "agents", "/api/agents/control-plane", exc)
        control_plane = control_plane if isinstance(control_plane, dict) else {}
        agent_rows = control_plane.get("agents", {})
        if not isinstance(agent_rows, dict):
            agent_rows = {}

        # 1. Inspect Strategy Lab (Cockpit)
        cockpit_data: Dict[str, Any] = {}
        try:
            # Cockpit aggregates several bounded stores and can legitimately
            # exceed the generic 10s provider timeout during a warm start.
            cockpit_data = self._fetch_json(f"{base_url}/api/cockpit", timeout=30.0)
            strat_lab = cockpit_data.get("strategy_lab", {}) if isinstance(cockpit_data, dict) else {}
            records = strat_lab.get("records", []) if isinstance(strat_lab, dict) else []
            
            unavail_count = sum(
                1 for r in records
                if r.get("oos_verdict") in {None, "", "UNAVAILABLE"} or r.get("last_validated") in {None, "", "—", "-"}
            )
            total_records = len(records)
            if total_records > 0 and unavail_count == total_records:
                anomalies.append({
                    "tab": "Strategy Lab",
                    "kind": "DATA_STALL",
                    "title": "Strategy Lab OOS / Robustness missing across all active strategies",
                    "problem": (
                        f"UI Inspector: In Strategy Lab, 100% of strategy records ({total_records}) "
                        f"render OOS Verdict as UNAVAILABLE or missing last_validated timestamps. "
                        f"The Strategy Research Agent evidence is not being surfaced to the Cockpit API. "
                        f"Investigate api/cockpit_service.py _strategy_lab and ensure research performance cache is connected."
                    ),
                    "context": {"unavail_count": unavail_count, "total": total_records},
                })
                tab_health["strategy_lab"] = {"status": "DEGRADED", "issue": f"{unavail_count}/{total_records} unvalidated"}
            else:
                tab_health["strategy_lab"] = {"status": "HEALTHY", "records": total_records}
        except Exception as exc:
            endpoint_error("Cockpit", "strategy_lab", "/api/cockpit", exc)

        # 2. Inspect News Flow Tab
        try:
            news_data = self._fetch_json(f"{base_url}/api/news-tab/state")
            if isinstance(news_data, dict):
                count = int(news_data.get("total_articles", news_data.get("count", 0)) or 0)
                if count == 0 and isinstance(news_data.get("articles"), list):
                    count = len(news_data["articles"])
            else:
                count = 0
            if count == 0:
                anomalies.append({
                    "tab": "News Tab",
                    "kind": "EMPTY_FEED",
                    "title": "News flow feed is empty (0 articles)",
                    "problem": (
                        "UI Inspector: GET /api/news-tab/state returned 0 articles. "
                        "News flow is stalled or RSS/FinBERT ingest pipeline is interrupted. "
                        "Investigate agents/news_tab_agent.py and data/news_backup.json."
                    ),
                    "context": {"article_count": count},
                })
                tab_health["news_tab"] = {"status": "DEGRADED", "issue": "0 articles"}
            else:
                tab_health["news_tab"] = {"status": "HEALTHY", "articles": count}
            news_age = _iso_age_seconds(news_data.get("last_update"))
            if news_age is not None:
                tab_health["news_tab"]["last_update_age_seconds"] = round(news_age)
                if news_age > _NEWS_RADAR_STALE_SECONDS and not news_data.get("disabled"):
                    tab_health["news_tab"]["status"] = "DEGRADED"
                    tab_health["news_tab"]["issue"] = (
                        f"feed frozen for {int(news_age)}s (> {_NEWS_RADAR_STALE_SECONDS:.0f}s budget)"
                    )
                    anomalies.append({
                        "tab": "News Tab",
                        "kind": "DATA_STALE",
                        "title": "News flow feed is not refreshing",
                        "problem": (
                            f"UI Inspector: /api/news-tab/state last_update is {int(news_age)}s old "
                            f"(budget {_NEWS_RADAR_STALE_SECONDS:.0f}s). Articles exist but the ingest "
                            "pipeline (RSS/FinBERT refresh loop) has stalled — the tab shows old news "
                            "without any visual staleness marker. Investigate the refresh loop and "
                            "last_update publication in agents/news_tab_agent.py."
                        ),
                        "context": {
                            "last_update_age_seconds": round(news_age),
                            "article_count": count,
                            "file": "agents/news_tab_agent.py",
                            "allowed_files": _AGENT_REPAIR_FILES["news_tab"],
                        },
                    })
        except Exception as exc:
            endpoint_error("News Tab", "news_tab", "/api/news-tab/state", exc)

        # 3. Inspect Market Scanner Tab
        try:
            scanner_data = self._fetch_json(f"{base_url}/api/scanner/state")
            hotlist = scanner_data.get("hot_tickers", scanner_data.get("hotlist", [])) if isinstance(scanner_data, dict) else []
            crypto = scanner_data.get("liquid_crypto_tickers", scanner_data.get("crypto_pairs", [])) if isinstance(scanner_data, dict) else []
            if not hotlist and not crypto:
                anomalies.append({
                    "tab": "Market Scanner",
                    "kind": "EMPTY_FEED",
                    "title": "Market Scanner returned no stock or crypto movers",
                    "problem": (
                        "UI Inspector: Scanner state is completely empty. Both stock hotlist and "
                        "crypto pairs have 0 entries. Investigate scanner background worker and exchange feeds."
                    ),
                    "context": {"hotlist_len": len(hotlist), "crypto_len": len(crypto)},
                })
                tab_health["scanner"] = {"status": "DEGRADED", "issue": "empty scanner feed"}
            else:
                tab_health["scanner"] = {"status": "HEALTHY", "stocks": len(hotlist), "crypto": len(crypto)}
            scanner_age = _iso_age_seconds(scanner_data.get("as_of_utc"))
            if scanner_age is not None and not scanner_data.get("disabled"):
                scanner_refresh = scanner_data.get("refresh_seconds") or 60
                scanner_budget = _staleness_threshold(scanner_refresh)
                tab_health["scanner"]["payload_age_seconds"] = round(scanner_age)
                if scanner_age > scanner_budget:
                    tab_health["scanner"]["status"] = "DEGRADED"
                    tab_health["scanner"]["issue"] = (
                        f"scan frozen for {int(scanner_age)}s (> {int(scanner_budget)}s budget)"
                    )
                    anomalies.append({
                        "tab": "Market Scanner",
                        "kind": "DATA_STALE",
                        "title": "Market Scanner data is frozen",
                        "problem": (
                            f"UI Inspector: /api/scanner/state as_of_utc is {int(scanner_age)}s old "
                            f"(budget {int(scanner_budget)}s at refresh_seconds={scanner_refresh}). "
                            "Movers exist but are stale — positions sized off them read wrong prices. "
                            "Investigate the scanner worker cadence and exchange feed errors."
                        ),
                        "context": {
                            "payload_age_seconds": round(scanner_age),
                            "refresh_seconds": scanner_refresh,
                            "file": "agents/scanner_agent.py",
                            "allowed_files": _AGENT_REPAIR_FILES["scanner"],
                        },
                    })
        except Exception as exc:
            endpoint_error("Market Scanner", "scanner", "/api/scanner/state", exc)

        # 4. Inspect Trade Radar Tab
        try:
            radar_data = self._fetch_json(f"{base_url}/api/traderadar/state?limit=40")
            items = radar_data.get("candidates", radar_data.get("items", [])) if isinstance(radar_data, dict) else []
            tab_health["trade_radar"] = {"status": "HEALTHY" if items else "WATCH", "candidates": len(items)}
            radar_age = _iso_age_seconds(radar_data.get("as_of") if isinstance(radar_data, dict) else None)
            if radar_age is not None and not radar_data.get("disabled"):
                tab_health["trade_radar"]["payload_age_seconds"] = round(radar_age)
                if radar_age > _NEWS_RADAR_STALE_SECONDS:
                    tab_health["trade_radar"]["status"] = "DEGRADED"
                    tab_health["trade_radar"]["issue"] = (
                        f"radar frozen for {int(radar_age)}s (> {_NEWS_RADAR_STALE_SECONDS:.0f}s budget)"
                    )
                    anomalies.append({
                        "tab": "Trade Radar",
                        "kind": "DATA_STALE",
                        "title": "Trade Radar candidates are frozen",
                        "problem": (
                            f"UI Inspector: /api/traderadar/state as_of is {int(radar_age)}s old "
                            f"(budget {_NEWS_RADAR_STALE_SECONDS:.0f}s). Candidates render but the radar "
                            "engine stopped regenerating them — entries/stops may be evaluated on old "
                            "levels. Investigate the radar agent cycle and its publish path."
                        ),
                        "context": {
                            "payload_age_seconds": round(radar_age),
                            "candidates": len(items),
                            "file": "agents/trade_radar_agent.py",
                            "allowed_files": _AGENT_REPAIR_FILES["trade_radar"],
                        },
                    })
        except Exception as exc:
            endpoint_error("Trade Radar", "trade_radar", "/api/traderadar/state", exc)

        # 5. Inspect Pre-Earnings Tab — beyond item presence: payload freshness,
        # cycle liveness, partial-update masking, and Pulse population vs the
        # durable pick registry. (A 200-row snapshot frozen for hours with an
        # empty Pulse card must never read as HEALTHY again.)
        try:
            pe_data = self._fetch_json(f"{base_url}/api/pre-earnings")
            if not isinstance(pe_data, dict):
                pe_data = {}
            pe_items = pe_data.get("items") or []
            pe_status = pe_data.get("status") if isinstance(pe_data.get("status"), dict) else {}
            pe_disabled = bool(pe_data.get("disabled"))
            try:
                pe_refresh = max(30, int(pe_data.get("refresh_seconds") or 600))
            except (TypeError, ValueError):
                pe_refresh = 600
            pe_issues: List[str] = []
            pe_health: Dict[str, Any] = {"earnings_monitored": len(pe_items)}
            pe_repair_files = _AGENT_REPAIR_FILES["pre_earnings"]

            payload_age = _iso_age_seconds(pe_data.get("as_of_utc"))
            last_run_age = _iso_age_seconds(pe_status.get("last_run_time"))
            last_success_age = _iso_age_seconds(pe_status.get("last_success_time"))
            stale_budget = _staleness_threshold(pe_refresh)

            if not pe_disabled and payload_age is not None:
                pe_health["payload_age_seconds"] = round(payload_age)
                if payload_age > stale_budget:
                    pe_issues.append(
                        f"payload frozen for {int(payload_age)}s (> {int(stale_budget)}s budget)"
                    )
                    anomalies.append({
                        "tab": "Pre-Earnings",
                        "kind": "DATA_STALE",
                        "title": "Pre-Earnings tab serves a stale snapshot",
                        "problem": (
                            f"UI Inspector: /api/pre-earnings as_of_utc is {int(payload_age)}s old "
                            f"(budget {int(stale_budget)}s at refresh_seconds={pe_refresh}) while the "
                            "agent is enabled. The full scan cycle is not completing or its result is "
                            "not being published. Investigate the run loop, provider fetch failures, "
                            "and payload persistence in agents/pre_earnings_agent.py."
                        ),
                        "context": {
                            "payload_age_seconds": round(payload_age),
                            "refresh_seconds": pe_refresh,
                            "file": pe_repair_files[0],
                            "allowed_files": pe_repair_files,
                        },
                    })
            if not pe_disabled and last_run_age is not None:
                pe_health["last_cycle_age_seconds"] = round(last_run_age)
                if last_run_age > stale_budget:
                    pe_issues.append(
                        f"no full cycle started for {int(last_run_age)}s"
                    )
                    anomalies.append({
                        "tab": "Pre-Earnings",
                        "kind": "CYCLE_STALLED",
                        "title": "Pre-Earnings full scan cycle is stalled",
                        "problem": (
                            f"UI Inspector: status.last_run_time is {int(last_run_age)}s old while the "
                            "agent is enabled — the loop thread never enters run_cycle. Check for a "
                            "starved scheduler (partial updates deferring the next full cycle), a "
                            "deadlocked lock, or a hung provider call in agents/pre_earnings_agent.py."
                        ),
                        "context": {
                            "last_cycle_age_seconds": round(last_run_age),
                            "refresh_seconds": pe_refresh,
                            "file": pe_repair_files[0],
                            "allowed_files": pe_repair_files,
                        },
                    })
            # Partial-update masking: targeted rescorses / coverage warmups keep
            # refreshing last_success_time while no full payload is published.
            # This exact divergence hid a multi-hour scheduler starvation.
            if (
                not pe_disabled
                and payload_age is not None
                and last_success_age is not None
                and last_success_age + 2 * pe_refresh < payload_age
            ):
                pe_issues.append(
                    f"last_success is {int(payload_age - last_success_age)}s newer than the payload"
                )
                anomalies.append({
                    "tab": "Pre-Earnings",
                    "kind": "PARTIAL_UPDATE_MASKING",
                    "title": "Pre-Earnings partial updates mask a frozen full snapshot",
                    "problem": (
                        f"UI Inspector: status.last_success_time is {int(payload_age - last_success_age)}s "
                        "newer than as_of_utc. Targeted rescorses / coverage warmups advance the success "
                        "clock while the full-cycle payload never rebuilds, so freshness guards and UI "
                        "polling read healthy. Verify the full-cycle scheduler is not anchored on a "
                        "timestamp that partial updates can refresh."
                    ),
                    "context": {
                        "payload_age_seconds": round(payload_age),
                        "last_success_age_seconds": round(last_success_age),
                        "refresh_seconds": pe_refresh,
                        "file": pe_repair_files[0],
                        "allowed_files": pe_repair_files,
                    },
                })
            # Pulse card: the live picks must exist whenever the durable
            # registry still holds active picks for upcoming events.
            pulse = pe_data.get("pulse") if isinstance(pe_data.get("pulse"), dict) else {}
            current_long = pulse.get("current_top_long") if isinstance(pulse.get("current_top_long"), list) else []
            current_short = pulse.get("current_top_short") if isinstance(pulse.get("current_top_short"), list) else []
            active_history = pulse.get("active_history") if isinstance(pulse.get("active_history"), dict) else {}
            union_long = pulse.get("top_long") if isinstance(pulse.get("top_long"), list) else []
            union_short = pulse.get("top_short") if isinstance(pulse.get("top_short"), list) else []
            has_current_picks = bool(current_long or current_short)
            has_active_picks = bool(
                (active_history.get("top_long") if isinstance(active_history.get("top_long"), list) else [])
                or (active_history.get("top_short") if isinstance(active_history.get("top_short"), list) else [])
                or union_long
                or union_short
            )
            pe_health["pulse_current_picks"] = len(current_long) + len(current_short)
            pe_health["pulse_active_picks"] = len(union_long) + len(union_short)
            if pulse and not has_current_picks and has_active_picks:
                pe_issues.append(
                    f"Pulse card empty while {len(union_long) + len(union_short)} active pick(s) exist"
                )
                anomalies.append({
                    "tab": "Pre-Earnings",
                    "kind": "PULSE_EMPTY_WITH_ACTIVE_PICKS",
                    "title": "Pre-Earnings Pulse renders empty while active picks exist",
                    "problem": (
                        f"UI Inspector: pulse.current_top_long/top_short are both empty but the durable "
                        f"Pulse registry holds {len(union_long)} active long and {len(union_short)} active "
                        "short pick(s). The live card shows the 'no setup clears the gates' empty state "
                        "for events that are still upcoming. Investigate the pulse store view "
                        "(latest snapshot vs active union) and cross-process sidecar reload in "
                        "agents/pre_earnings_agent.py."
                    ),
                    "context": {
                        "active_long": [str(p.get("symbol") or "") for p in union_long if isinstance(p, dict)],
                        "active_short": [str(p.get("symbol") or "") for p in union_short if isinstance(p, dict)],
                        "pulse_generated_at": pulse.get("generated_at"),
                        "file": pe_repair_files[0],
                        "allowed_files": pe_repair_files,
                    },
                })
            pe_health["status"] = "DEGRADED" if pe_issues else ("HEALTHY" if pe_items else "WATCH")
            if pe_issues:
                pe_health["issues"] = pe_issues
            tab_health["pre_earnings"] = pe_health
        except Exception as exc:
            endpoint_error("Pre-Earnings", "pre_earnings", "/api/pre-earnings", exc)

        # 5b. Inspect Premarket Tab (skipped while the agent is disabled or the
        # payload carries no machine-readable freshness contract).
        try:
            premarket_data = self._fetch_json(f"{base_url}/api/premarket")
            if not isinstance(premarket_data, dict):
                premarket_data = {}
            if premarket_data.get("disabled"):
                pm_status = (
                    premarket_data.get("status")
                    if isinstance(premarket_data.get("status"), dict)
                    else {}
                )
                pm_reason = str(pm_status.get("reason") or "")
                premarket_row = agent_rows.get("premarket_scanner")
                operator_enabled = bool(
                    premarket_row.get("enabled") if isinstance(premarket_row, dict) else False
                )
                if pm_reason == "market_open_paused" and operator_enabled:
                    # Session pause, not a defect: the premarket scanner is
                    # intentionally idle while the market is open. Recorded
                    # as a note so the fleet view stays green.
                    tab_health["premarket"] = {
                        "status": "HEALTHY",
                        "note": "paused during market session",
                    }
                else:
                    tab_health["premarket"] = {"status": "WATCH", "issue": "agent disabled"}
            else:
                pm_candidates = (
                    premarket_data.get("candidates")
                    or premarket_data.get("long_candidates")
                    or []
                )
                pm_refresh = premarket_data.get("refresh_seconds") or 300
                pm_budget = _staleness_threshold(pm_refresh)
                pm_age = _iso_age_seconds(premarket_data.get("as_of_utc"))
                pm_health = {
                    "status": "HEALTHY" if pm_candidates else "WATCH",
                    "candidates": len(pm_candidates),
                }
                if pm_age is not None:
                    pm_health["payload_age_seconds"] = round(pm_age)
                    if pm_age > pm_budget:
                        pm_health["status"] = "DEGRADED"
                        pm_health["issue"] = (
                            f"snapshot frozen for {int(pm_age)}s (> {int(pm_budget)}s budget)"
                        )
                        anomalies.append({
                            "tab": "Premarket",
                            "kind": "DATA_STALE",
                            "title": "Premarket tab serves a stale snapshot",
                            "problem": (
                                f"UI Inspector: /api/premarket as_of_utc is {int(pm_age)}s old "
                                f"(budget {int(pm_budget)}s at refresh_seconds={pm_refresh}) while the "
                                "agent is enabled. Investigate the premarket scan loop and provider "
                                "fetch failures in agents/premarket_scanner_agent.py."
                            ),
                            "context": {
                                "payload_age_seconds": round(pm_age),
                                "refresh_seconds": pm_refresh,
                                "file": "agents/premarket_scanner_agent.py",
                                "allowed_files": _AGENT_REPAIR_FILES["premarket_scanner"],
                            },
                        })
                tab_health["premarket"] = pm_health

                # 5c. Premarket field coverage (2026-08 audit): the details
                # panel promises Earnings Date, News Count, Vol Rel, Catalyst/
                # Liq and the AI "Why Moving" read. Inputs can be healthy
                # (flags available, caches fresh) while the derived fields are
                # systematically empty, so every card reads "n/a" and the gap
                # goes unnoticed (live: vol_rel 0/15, news 4/15). Only slates
                # of 5+ candidates are scored — smaller samples cannot tell a
                # systemic gap from honest absence (a name simply has no
                # upcoming earnings most days).
                pm_rows = []
                for key in ("long_candidates", "short_candidates", "watch_only", "candidates"):
                    for row in (premarket_data.get(key) or []):
                        if isinstance(row, dict):
                            pm_rows.append(row)
                seen_syms = set()
                pm_slate = []
                for row in pm_rows:
                    sym = str(row.get("symbol") or "").upper().strip()
                    if not sym or sym in seen_syms:
                        continue
                    seen_syms.add(sym)
                    pm_slate.append(row)
                if len(pm_slate) >= 5:
                    def _has_earnings(row: Dict[str, Any]) -> bool:
                        earnings = row.get("earnings") if isinstance(row.get("earnings"), dict) else {}
                        return bool(
                            earnings.get("date")
                            or any(
                                earnings.get(k) is not None
                                for k in ("eps_estimated", "eps_actual", "revenue_estimated", "revenue_actual")
                            )
                        )

                    slate_n = len(pm_slate)
                    coverage = {
                        "slate": slate_n,
                        "earnings_date": sum(1 for r in pm_slate if _has_earnings(r)),
                        "news_items": sum(1 for r in pm_slate if isinstance(r.get("news"), list) and r["news"]),
                        "vol_rel": sum(1 for r in pm_slate if r.get("premarket_vol_rel") is not None),
                        "why_moving_ai": sum(
                            1 for r in pm_slate
                            if isinstance(r.get("ollama"), dict) and str(r["ollama"].get("summary") or "").strip()
                        ),
                    }
                    pm_health["field_coverage"] = {
                        key: f"{value}/{slate_n}" for key, value in coverage.items() if key != "slate"
                    }
                    pm_flags = premarket_data.get("flags") if isinstance(premarket_data.get("flags"), dict) else {}
                    if coverage["vol_rel"] == 0:
                        explicitly_unavailable = all(
                            bool(row.get("premarket_vol_rel_unavailable")) for row in pm_slate
                        )
                        if explicitly_unavailable:
                            # This is a transparent provider limitation, not a
                            # broken field contract.  Do not ask the repair
                            # watchdog to fabricate regular-session volume as
                            # pre-market volume in order to make coverage green.
                            pm_health["status"] = "UNAVAILABLE"
                            pm_health["issue"] = "Extended-session volume unavailable from configured providers"
                        else:
                            pm_health["status"] = "DEGRADED"
                            pm_health["issue"] = "Vol Rel missing for every candidate"
                            anomalies.append({
                                "tab": "Premarket",
                                "kind": "DATA_INCOMPLETE",
                                "title": "Premarket relative volume is missing for the whole slate",
                                "problem": (
                                    f"UI Inspector: 0/{slate_n} premarket candidates expose premarket_vol_rel, "
                                    "but at least one candidate does not explicitly declare the metric unavailable. "
                                    "Investigate the movers fetch contract and the volume plumbing in "
                                    "agents/premarket_scanner_agent.py / agents/scanner_agent.py."
                                ),
                                "context": {
                                    "field_coverage": pm_health["field_coverage"],
                                    "file": "agents/premarket_scanner_agent.py",
                                    "allowed_files": _AGENT_REPAIR_FILES["premarket_scanner"],
                                },
                            })
                    news_ratio = coverage["news_items"] / slate_n
                    if (
                        news_ratio < 0.3
                        and pm_flags.get("news_available")
                        and not pm_flags.get("news_stale")
                    ):
                        pm_health["status"] = "DEGRADED"
                        pm_health["issue"] = f"news coverage {coverage['news_items']}/{slate_n}"
                        anomalies.append({
                            "tab": "Premarket",
                            "kind": "DATA_INCOMPLETE",
                            "title": "Premarket news coverage gap",
                            "problem": (
                                f"UI Inspector: only {coverage['news_items']}/{slate_n} premarket candidates "
                                "carry any news items while the news input reports available and fresh — "
                                "the details panel shows 'News Count: 0' for the rest and the news score "
                                "contributes nothing. Investigate per-ticker news hydration in "
                                "agents/news_tab_agent.py and its selection in the premarket pipeline."
                            ),
                            "context": {
                                "field_coverage": pm_health["field_coverage"],
                                "news_ratio": round(news_ratio, 2),
                                "file": "agents/news_tab_agent.py",
                                "allowed_files": _AGENT_REPAIR_FILES["news_tab"],
                            },
                        })
        except Exception as exc:
            endpoint_error("Premarket", "premarket", "/api/premarket", exc)

        # 6. Inspect the actual Agent Fleet lifecycle, not only cached timestamps.
        try:
            bot_state = control_plane.get("bot", {}) if isinstance(control_plane, dict) else {}
            engine_running = bool(bot_state.get("engine_running")) if isinstance(bot_state, dict) else False
            engine_managed = {"execution", "risk", "scanner", "sentiment"}
            stopped = []
            paused_with_engine = []
            for name, details in (agent_rows.items() if isinstance(agent_rows, dict) else []):
                if not isinstance(details, dict):
                    continue
                if not details.get("enabled") or details.get("initializing"):
                    continue
                last_error = str(details.get("last_error") or "").strip()
                # A scheduled pause (premarket scanner while the market is open,
                # etc.) is published through last_error but is not a failure;
                # escalating it opened a Coding Agent case on every scan.
                if last_error.lower() in {"market_open_paused", "outside_schedule", "market_closed"}:
                    last_error = ""
                if details.get("running") and last_error:
                    anomalies.append({
                        "tab": "Agents",
                        "kind": "AGENT_RUNTIME_ERROR",
                        "title": f"Enabled agent '{name}' reports a runtime error",
                        "problem": (
                            f"UI Inspector: {name} is alive but its last cycle failed: {last_error[:300]}. "
                            "Investigate the recorded failure path and verify a successful recovery cycle; "
                            "thread liveness alone is not a healthy state."
                        ),
                        "context": _agent_incident_context(name, details),
                    })
                    continue
                if details.get("running"):
                    # Thread alive but no completed cycle / heartbeat for several
                    # of its own intervals (control plane ``overdue``): the Agents
                    # tab flags it, so the inspector must not call the fleet healthy.
                    if details.get("overdue") is True:
                        anomalies.append({
                            "tab": "Agents",
                            "kind": "AGENT_OVERDUE",
                            "title": f"Enabled agent '{name}' is running but overdue",
                            "problem": (
                                f"UI Inspector: {name} is alive but "
                                f"{str(details.get('health_reason') or 'has not completed a cycle on schedule')[:300]}. "
                                "Investigate a hung provider call, a blocked lock or a starved scheduler in its loop."
                            ),
                            "context": _agent_incident_context(name, details),
                        })
                    continue
                if name in engine_managed and not engine_running:
                    paused_with_engine.append(name)
                    continue
                stopped.append(name)
                recovery_error = ""
                recovered = False
                try:
                    recovery = self._post_json(
                        f"{base_url}/api/agents/{name}/enabled",
                        {"enabled": True},
                    )
                    runtime = recovery.get("runtime", {}) if isinstance(recovery, dict) else {}
                    recovered = bool(isinstance(runtime, dict) and runtime.get("running"))
                except Exception as exc:
                    recovery_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                incident = {
                    "tab": "Agents",
                    "kind": "AGENT_STOPPED",
                    "title": f"Enabled agent '{name}' was not running",
                    "problem": (
                        f"UI Inspector: Agent Fleet reports {name} enabled=true but running=false. "
                        "The bounded runtime restart did not recover it; investigate lifecycle wiring, "
                        "startup reconciliation, and swallowed worker errors."
                    ),
                    "context": {
                        **_agent_incident_context(name, details),
                        "recovery_error": recovery_error,
                    },
                    "resolved": recovered,
                }
                anomalies.append(incident)
                if recovered:
                    self_healed.append({"agent": name, "action": "runtime_restart"})
            unresolved = [a for a in anomalies if a.get("tab") == "Agents" and not a.get("resolved")]
            tab_health["agents"] = {
                "status": "DEGRADED" if unresolved else ("RECOVERED" if stopped else "HEALTHY"),
                "enabled": sum(1 for value in agent_rows.values() if isinstance(value, dict) and value.get("enabled")),
                "running": sum(1 for value in agent_rows.values() if isinstance(value, dict) and value.get("running")),
                "stopped_detected": stopped,
                "paused_with_engine": paused_with_engine,
                "self_healed": [item["agent"] for item in self_healed],
            }
        except Exception as exc:
            endpoint_error("Agents", "agents", "/api/agents/control-plane", exc)

        # 7. Verify that executable AI decisions expose the independent Critic.
        try:
            ledger = cockpit_data.get("decision_ledger", {}) if isinstance(cockpit_data, dict) else {}
            ledger_rows = ledger.get("items", []) if isinstance(ledger, dict) else []
            missing_critic = []
            active_policy_violations = []
            historical_policy_violations = []
            for row in ledger_rows if isinstance(ledger_rows, list) else []:
                if not isinstance(row, dict):
                    continue
                status = str(row.get("status") or "").strip().lower()
                action = str(row.get("action") or row.get("what_happened") or "").upper()
                ai_model = row.get("ai_model") if isinstance(row.get("ai_model"), dict) else {}
                critic = row.get("critic") if isinstance(row.get("critic"), dict) else {}
                validation = row.get("validation") if isinstance(row.get("validation"), dict) else {}
                executable = action.startswith("OPEN_") or action in {"BUY", "SELL", "LONG", "SHORT"}
                executed = executable and status in {"executed", "open", "closed"}
                if executed and validation.get("ok") is False:
                    violation = {
                        "decision_id": row.get("decision_id"),
                        "signal_id": row.get("signal_id"),
                        "ticker": row.get("ticker"),
                        "status": status,
                        "validation_stage": validation.get("stage"),
                        "validation_reason": validation.get("reason"),
                    }
                    try:
                        raw_timestamp = str(row.get("timestamp") or "").strip().replace("Z", "+00:00")
                        row_epoch = datetime.fromisoformat(raw_timestamp).timestamp() if raw_timestamp else 0.0
                    except (TypeError, ValueError):
                        row_epoch = 0.0
                    if row_epoch and row_epoch < self._started_at_epoch:
                        historical_policy_violations.append(violation)
                    else:
                        active_policy_violations.append(violation)
                if (
                    executable
                    and status in {"accepted", "executed", "open", "closed"}
                    and ai_model.get("consulted") is not False
                    and validation.get("ok") is not False
                    and critic.get("available") is not True
                ):
                    missing_critic.append({
                        "decision_id": row.get("decision_id"),
                        "signal_id": row.get("signal_id"),
                        "ticker": row.get("ticker"),
                        "status": status,
                    })
            if missing_critic:
                anomalies.append({
                    "tab": "Cockpit / Decision Ledger",
                    "kind": "MISSING_CRITIC_EVIDENCE",
                    "title": "Executable AI decisions render Critic as unavailable",
                    "problem": (
                        f"UI Inspector: {len(missing_critic)} accepted/executed AI decision(s) do not expose "
                        "Critic evidence in the Cockpit. Trace ai_critic from the signal and Capital "
                        "decision_context through the Decision Ledger read model and UI adapter."
                    ),
                    "context": {
                        "decisions": missing_critic[:10],
                        "file": "api/cockpit_service.py",
                        "allowed_files": [
                            "api/cockpit_service.py",
                            "core/decision_ledger/types.py",
                            "core/agent_manager.py",
                        ],
                    },
                })
            if active_policy_violations:
                anomalies.append({
                    "tab": "Cockpit / Decision Ledger",
                    "kind": "EXECUTED_AFTER_AI_VALIDATION_VETO",
                    "title": "A trade executed after the AI validation gate vetoed it",
                    "problem": (
                        f"UI Inspector: {len(active_policy_violations)} current-process decision(s) reached execution "
                        "although AI validation returned a veto. The order path must fail closed on validation.ok=false."
                    ),
                    "context": {
                        "decisions": active_policy_violations[:10],
                        "file": "core/agent_manager.py",
                        "allowed_files": [
                            "core/agent_manager.py",
                            "core/decision_ledger/recorder.py",
                            "api/cockpit_service.py",
                        ],
                    },
                })
            ledger_degraded = bool(missing_critic or active_policy_violations)
            tab_health["decision_ledger"] = {
                "status": "DEGRADED" if ledger_degraded else ("RECOVERED" if historical_policy_violations else "HEALTHY"),
                "decisions_checked": len(ledger_rows) if isinstance(ledger_rows, list) else 0,
                "missing_critic": len(missing_critic),
                "active_policy_violations": len(active_policy_violations),
                "historical_policy_violations": len(historical_policy_violations),
                "guard_active_since": datetime.fromtimestamp(self._started_at_epoch, timezone.utc).isoformat(),
            }
        except Exception as exc:
            endpoint_error("Cockpit / Decision Ledger", "decision_ledger", "/api/cockpit", exc)

        # 7b. Inspect Capital tab — read-model liveness and decision freshness.
        try:
            cap_data = self._fetch_json(f"{base_url}/api/capital/decisions?limit=5")
            cap_records = cap_data.get("records", []) if isinstance(cap_data, dict) else []
            tab_health["capital"] = {
                "status": "HEALTHY",
                "decision_records": len(cap_records) if isinstance(cap_records, list) else 0,
            }
            cap_age = _iso_age_seconds(cap_data.get("as_of") if isinstance(cap_data, dict) else None)
            if cap_age is not None:
                tab_health["capital"]["payload_age_seconds"] = round(cap_age)
                if cap_age > _NEWS_RADAR_STALE_SECONDS:
                    tab_health["capital"]["status"] = "DEGRADED"
                    tab_health["capital"]["issue"] = (
                        f"capital decisions frozen for {int(cap_age)}s (> {_NEWS_RADAR_STALE_SECONDS:.0f}s budget)"
                    )
                    anomalies.append({
                        "tab": "Capital",
                        "kind": "DATA_STALE",
                        "title": "Capital tab serves stale decision records",
                        "problem": (
                            f"UI Inspector: /api/capital/decisions as_of is {int(cap_age)}s old "
                            f"(budget {_NEWS_RADAR_STALE_SECONDS:.0f}s). Positions and P/L render on a "
                            "frozen read model — equity, exposure and stop distances may be evaluated "
                            "on outdated prices. Investigate the capital decision pipeline and its "
                            "as_of publication path."
                        ),
                        "context": {"payload_age_seconds": round(cap_age), "records": len(cap_records)},
                    })
        except Exception as exc:
            endpoint_error("Capital", "capital", "/api/capital/decisions", exc)


        # 7d. Inspect Watchlist tab — endpoint liveness and non-empty registry.
        try:
            wl_data = self._fetch_json(f"{base_url}/api/watchlist")
            wl_stocks = wl_data.get("stocks") if isinstance(wl_data, dict) else None
            wl_crypto = wl_data.get("crypto") if isinstance(wl_data, dict) else None
            wl_hot = wl_data.get("hot_list") if isinstance(wl_data, dict) else None
            stocks_n = len(wl_stocks) if isinstance(wl_stocks, list) else 0
            crypto_n = len(wl_crypto) if isinstance(wl_crypto, list) else 0
            hot_n = len(wl_hot) if isinstance(wl_hot, list) else 0
            tab_health["watchlist"] = {
                "status": "HEALTHY",
                "stocks": stocks_n,
                "crypto": crypto_n,
                "hot_list": hot_n,
            }
            if stocks_n == 0 and crypto_n == 0 and hot_n == 0:
                tab_health["watchlist"]["status"] = "DEGRADED"
                tab_health["watchlist"]["issue"] = "watchlist registry completely empty"
                anomalies.append({
                    "tab": "Watchlist",
                    "kind": "EMPTY_FEED",
                    "title": "Watchlist registry returned no tickers at all",
                    "problem": (
                        "UI Inspector: GET /api/watchlist returned 0 stocks, 0 crypto and 0 hot_list "
                        "entries. An owner-curated registry is never legitimately empty — this is a "
                        "persistence or deserialization regression. Investigate the watchlist store "
                        "and schema_version migration path."
                    ),
                    "context": {"stocks": stocks_n, "crypto": crypto_n, "hot_list": hot_n},
                })
        except Exception as exc:
            endpoint_error("Watchlist", "watchlist", "/api/watchlist", exc)

        # 8. Escalate unresolved anomalies to Coding Agent & Critic Watchdog.
        escalations_sent = 0
        unresolved_anomalies = [item for item in anomalies if not item.get("resolved")]
        if unresolved_anomalies:
            escalations_sent = self._escalate_to_coding_agent(unresolved_anomalies)

        duration_ms = (time.perf_counter() - started_at) * 1000.0
        report = {
            "status": "ok",
            "last_scan_at": now_iso,
            "duration_ms": round(duration_ms, 2),
            "tabs_checked": len(tab_health),
            "anomalies_detected": len(anomalies),
            "unresolved_anomalies": len(unresolved_anomalies),
            "escalations_sent": escalations_sent,
            "self_healed_count": len(self_healed),
            "self_healed": self_healed,
            "tab_health": tab_health,
            "anomalies": anomalies,
        }
        with self._lock:
            self._last_report = report

        update_agent_status(
            "ui_inspector",
            enabled=True,
            last_run=now_iso,
            last_success=now_iso,
            last_error=None,
            avg_duration_ms=round(duration_ms, 2),
            anomalies_detected=len(anomalies),
            unresolved_anomalies=len(unresolved_anomalies),
            escalations_sent=escalations_sent,
            self_healed_count=len(self_healed),
        )
        return report

    _LAYOUT_DEDUP_SECONDS = 30 * 60

    # Overflow fixes live in stylesheets — give the Coding Agent the owning
    # file as incident scope so its diff is anchored to real CSS content.
    _VIEW_STYLESHEETS = {
        "capital": ["frontend/styles_capital.css"],
        "scanner": ["frontend/styles_scanner.css"],
        "cockpit": ["frontend/styles_cockpit.css"],
        "preearnings": ["frontend/styles_preearnings.css"],
        "premarket": ["frontend/styles_premarket.css"],
        "agents": ["frontend/styles_agents.css"],
        "alerts": ["frontend/styles_alerts.css"],
        "backtest": ["frontend/styles_backtest.css"],
        "traderadar": ["frontend/styles_traderadar.css"],
        "news": ["frontend/styles_news.css"],
    }
    _DEFAULT_STYLESHEETS = ["frontend/styles_premium.css", "frontend/styles_base.css"]

    def record_layout_report(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Accept a browser-side layout audit report and escalate new defects.

        The frontend scans the live DOM (text escaping its card, clipped
        content without ellipsis) and POSTs findings here; each distinct
        signature becomes a Coding Agent repair case, throttled to one per
        30 minutes so a persistent defect cannot spam the case store.
        """
        if not isinstance(payload, dict):
            return {"status": "ignored", "reason": "payload must be an object"}
        defects = payload.get("defects")
        if not isinstance(defects, list) or not defects:
            return {"status": "ignored", "reason": "no defects"}

        now = time.time()
        accepted: List[Dict[str, Any]] = []
        with self._lock:
            if len(self._layout_signatures) > 500:
                self._layout_signatures = {
                    sig: ts for sig, ts in self._layout_signatures.items()
                    if now - ts < 24 * 3600
                }
            for defect in defects[:10]:
                if not isinstance(defect, dict):
                    continue
                view = str(defect.get("view") or "unknown")[:40]
                selector = str(defect.get("selector") or "")[:160]
                text = str(defect.get("text") or "")[:160]
                try:
                    overflow_px = int(float(defect.get("overflow_px") or 0))
                except (TypeError, ValueError):
                    overflow_px = 0
                direction = str(defect.get("direction") or "right")[:10]
                signature = f"{view}|{selector}|{text[:60]}"
                if now - self._layout_signatures.get(signature, 0.0) < self._LAYOUT_DEDUP_SECONDS:
                    continue
                self._layout_signatures[signature] = now
                stylesheets = self._VIEW_STYLESHEETS.get(view, self._DEFAULT_STYLESHEETS)
                accepted.append({
                    "tab": f"{view} (layout)",
                    "kind": "LAYOUT_OVERFLOW",
                    "title": f"Text escapes its container in {view}",
                    "problem": (
                        f"UI Inspector layout audit (reported from the live browser DOM): element "
                        f"'{selector}' overflows its card by ~{overflow_px}px to the {direction} with "
                        f"text \"{text}\". The content visually crosses the container boundary instead "
                        "of wrapping or clipping. Fix the owning stylesheet: add overflow hidden plus "
                        "text-overflow ellipsis (with a title tooltip for the full value), allow "
                        "wrapping, or widen the grid cell — the text must stay inside its card."
                    ),
                    "context": {
                        "view": view,
                        "selector": selector,
                        "text_snippet": text,
                        "overflow_px": overflow_px,
                        "direction": direction,
                        "reported_by": "browser_layout_audit",
                        "file": stylesheets[0],
                        "allowed_files": stylesheets,
                    },
                })

        cases_created = self._escalate_to_coding_agent(accepted) if accepted else 0
        if accepted:
            logger.info(
                "UIInspectorAgent: layout audit accepted %d defect(s), %d case(s) created.",
                len(accepted), cases_created,
            )
        return {
            "status": "ok",
            "received": len(defects),
            "accepted": len(accepted),
            "cases_created": cases_created,
        }

    def _fetch_json(self, url: str, timeout: float = 10.0) -> Dict[str, Any]:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "UIInspectorAgent/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_json(self, url: str, payload: Dict[str, Any], timeout: float = 15.0) -> Dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "User-Agent": "UIInspectorAgent/1.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _escalate_to_coding_agent(self, anomalies: List[Dict[str, Any]]) -> int:
        """Enqueue anomalies into RepairCaseStore for Coding Agent auto-repair."""
        sent = 0
        try:
            from core.development_factory.watchdog import (
                CaseDraft,
                RepairCaseStore,
                _fingerprint,
                get_repair_watchdog,
            )

            store = RepairCaseStore()
            for anomaly in anomalies:
                draft = CaseDraft(
                    fingerprint=_fingerprint("ui_inspector", anomaly.get("tab", ""), anomaly.get("title", "")),
                    kind="RUNTIME",
                    source_agent="ui_inspector",
                    title=f"[UI Inspector] {anomaly.get('title')}",
                    problem=anomaly.get("problem", "UI Data stall defect detected."),
                    context=anomaly.get("context", {}),
                )
                enqueued = store.enqueue(draft)
                if enqueued:
                    sent += 1
                    logger.info(
                        f"UIInspectorAgent: Escalated anomaly in {anomaly.get('tab')} to Coding Agent (case_id={enqueued.get('case_id')})."
                    )

            if sent > 0:
                # Wake the coding watchdog to investigate and repair immediately
                watchdog = get_repair_watchdog(self.config)
                if watchdog:
                    watchdog.request_run()
        except Exception as exc:
            logger.error(f"UIInspectorAgent: Escalation to Coding Agent failed: {exc}")
        return sent

    def get_runtime_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "running": self.is_running,
                "last_scan_at": self._last_report.get("last_scan_at"),
                "tabs_checked": self._last_report.get("tabs_checked", 0),
                "anomalies_detected": self._last_report.get("anomalies_detected", 0),
                "unresolved_anomalies": self._last_report.get("unresolved_anomalies", 0),
                "self_healed_count": self._last_report.get("self_healed_count", 0),
                "tab_health": self._last_report.get("tab_health", {}),
            }

    def get_inspection_report(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._last_report)
