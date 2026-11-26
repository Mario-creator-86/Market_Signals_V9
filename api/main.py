"""
Market Signals V9 - Web API
FastAPI application for serving market signals data
"""

import logging
import copy
import json
import hmac
import math
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from numbers import Real
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, JSONResponse


def _safe_error(msg: str = "Internal server error") -> HTTPException:
    """Return a sanitized error that doesn't leak internal details."""
    return HTTPException(status_code=500, detail=msg)


def _raise_if_config_not_saved(config: Any, what: str) -> None:
    """Fail the request when the config write behind it did not reach config.json.

    ConfigManager records a failed write (`save_ok` False, `last_save_error`)
    instead of raising, and the services that call it do not look at the
    result, so "start bot", "save risk limits" etc. answered success although
    nothing was saved. Fakes without these attributes are treated as saved.
    """
    if getattr(config, "save_ok", True) is not False:
        return
    reason = str(getattr(config, "last_save_error", "") or "config.json could not be written")
    logger.error("%s was NOT saved: %s", what, reason)
    raise HTTPException(
        status_code=500,
        detail=(
            f"{what} was NOT saved: the settings file could not be written. The change is "
            "active only in this server process until a later save succeeds and is lost on restart."
        ),
    )


from contextlib import asynccontextmanager
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load .env file for API keys and secrets (before any config imports)
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

# Safety defaults to keep API responsive even when started via uvicorn/PyCharm.
os.environ.setdefault("MS_ENGINE_MODE", "subprocess")
os.environ.setdefault("MS_PROCESS_ROLE", "api")
os.environ.setdefault("MS_DISABLE_WARMUP", "1")
os.environ.setdefault("MS_DISABLE_STATE_PREFETCH", "1")

from api.state_service import get_capital_state, get_state, get_traderadar_state, normalize_signal_payload_scores
from api.dashboard_service import get_dashboard_payload
from api.cockpit_service import get_cockpit_payload
from api.config_service import ConfigValidationError, get_config, save_config
from api.ai_config_service import (
    ai_validated_request,
    discover_ai_models,
    get_ai_assignment,
    get_ai_router,
    list_ai_providers,
    probe_ai_provider,
    set_ai_assignment,
    set_ai_router,
)
from api.agents_service import (
    CONTROL_PLANE_AGENTS,
    RequiredAgentRunningError,
    RiskConstitutionSettingsError,
    UnknownAgentError,
    assert_no_risk_manager_owned_updates,
    build_control_plane_payload,
    set_agent_enabled,
    set_risk_constitution,
    set_risk_settings,
)
from api.agent_runtime_service import (
    AgentRuntimeError,
    set_agent_runtime,
    snapshot_agent_runtimes,
)
from api.actions_service import (
    add_ticker, remove_tickers, reorder_watchlist, 
    run_backtest,
    get_alerts, create_alert, delete_alert, toggle_alert,
    quick_analyze_ticker, preview_global_settings_apply,
    analyze_ticker_mode, get_strategy_config, save_strategy_config, get_strategy_performance,
)
from api.engine_service import get_engine_status, is_engine_running, start_engine, stop_engine
from api.sec_service import get_sec_context
from core.agent_toggles import is_agent_enabled
from core.api_traffic_control import (
    ApiTrafficProtectionMiddleware,
    ensure_scope_request_id,
    get_api_traffic_controller,
)
from core.cache_manager import cache_manager
from core.config import DATA_DIR, ConfigManager, is_crypto_ticker, normalize_market_ticker
from core.config import _write_json_atomic as _atomic_json_write
from core.data_quality import get_health_dashboard
from core.deployment_security import validate_deployment_security
from core.http_utils import snapshot_http_governor
from core.observability import (
    app_version,
    commit_hash,
    memory_snapshot,
    record_internal_request,
    snapshot_agent_status,
    snapshot_cache_decisions,
    snapshot_external_traces,
    snapshot_request_metrics,
    snapshot_latency_percentiles,
    snapshot_http_slo,
    snapshot_error_counts,
    snapshot_cache_hit_rates,
    update_agent_status,
    uptime_seconds,
)
from core.risk_limits import (
    MAX_DAILY_LOSS_PCT,
    MAX_RISK_PER_TRADE_PCT,
    MIN_DAILY_LOSS_PCT,
    MIN_RISK_PER_TRADE_PCT,
    RiskLimitValidationError,
)
from core.singleflight import (
    calendar_state_flight,
    hotlist_flight,
    premarket_flight,
    scanner_flight,
)
from core.request_context import RequestContext, clear_request_context, get_request_context, set_request_context
from core.state_persistence import (
    load_signals_payload,
    save_signals_payload,
    load_state_payload,
    save_state_payload,
    SQLITE_KEY_STATE,
    SQLITE_KEY_SIGNALS,
)
from core.sqlite_store import get_sqlite_store

# Import new modules
from core.hotlist_module import get_hotlist_module, HotlistCategory, HotlistItem

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("MarketSignalsAPI")

IS_TEST_MODE = (os.getenv("MS_TEST_MODE", "0") == "1" or str(os.getenv("TEST_MODE", "0")).strip().lower() in {"1", "true", "yes", "on"})
os.environ.setdefault("TEST_MODE", "1" if IS_TEST_MODE else "0")
_BOOT_DEPLOY_ENV = str(os.getenv("MS_ENV", "development") or "development").strip().lower()
_BOOT_IS_PRODUCTION = _BOOT_DEPLOY_ENV in {"production", "prod"}

_CACHE_GUARD_LOCK = threading.RLock()
_CACHE_GUARD_WINDOW_SEC = 30
_CACHE_GUARD_THRESHOLD = 10
_CACHE_GUARD_ENDPOINTS = {
    "/api/calendar/events",
    "/api/calendar/range",
    "/api/calendar/today",
    "/api/premarket",
    "/api/traderadar/state",
}
_CACHE_GUARD_HITS = {path: [] for path in _CACHE_GUARD_ENDPOINTS}
_DEFAULT_UI_CACHE_ONLY_ENDPOINTS = {
    "/api/cockpit",
    "/api/dashboard",
    "/api/premarket",
    "/api/pre-earnings",
    "/api/scanner/state",
    "/api/calendar/events",
    "/api/calendar/range",
    "/api/calendar/today",
    "/api/traderadar/state",
    "/api/state",
    "/api/news-tab/state",
}
_UI_CACHE_ONLY_ENDPOINTS = _DEFAULT_UI_CACHE_ONLY_ENDPOINTS | {
    p.strip()
    for p in str(os.getenv("MS_UI_CACHE_ONLY_ENDPOINTS", "")).split(",")
    if p.strip()
}
_SLOW_REQUEST_MS = max(50.0, float(os.getenv("MS_SLOW_REQUEST_MS", "500") or 500))
_RADAR_HINT_LOCK = threading.Lock()
_RADAR_HINT_TS = 0.0
_PREMARKET_DISK_STATE_PATH = DATA_DIR / "premarket.json"

# ==================== OLLAMA AUTO-START ====================
_OLLAMA_START_LOCK = threading.Lock()
_OLLAMA_START_ATTEMPTED = False
_OLLAMA_DEFAULT_EXE = os.getenv("OLLAMA_EXE", "")

def _get_ollama_base_url() -> str:
    try:
        from core.ollama_client import ollama
        ollama.refresh_config()
        return ollama.base_url.rstrip("/")
    except Exception:
        return "http://127.0.0.1:11434"

def _ollama_is_running(timeout_sec: float = 2.0) -> bool:
    try:
        from core.ollama_client import ollama
        return ollama.check_availability()
    except Exception:
        return False

def _start_ollama_process() -> bool:
    exe_env = os.getenv("OLLAMA_EXE")
    candidates = []
    if exe_env:
        candidates.append(exe_env)
    candidates.append(_OLLAMA_DEFAULT_EXE)
    candidates.append("ollama")

    for exe in candidates:
        try:
            if exe != "ollama" and not Path(exe).exists():
                continue

            creationflags = 0
            startupinfo = None
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            subprocess.Popen(
                [exe, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
            return True
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning(f"Failed to start Ollama using '{exe}': {e}")
            continue

    logger.warning("Ollama executable not found. Set OLLAMA_EXE env var to your ollama.exe path.")
    return False


@asynccontextmanager
async def lifespan(app_: FastAPI):
    deployment = validate_deployment_security()
    for warning in deployment.warnings:
        logger.warning(warning)
    from core.operator_audit import OperatorAuditError, get_operator_audit_ledger

    operator_audit = get_operator_audit_ledger()
    if operator_audit.required and not operator_audit.ensure_ready():
        raise OperatorAuditError(
            operator_audit.status().get("last_error")
            or "Production operator audit is not ready."
        )
    if not IS_TEST_MODE:
        # Before any agent starts: a restart must not hand out a fresh daily allowance of subscription calls.
        try:
            from core.ai.subscription_routing import remember_calls_in
            from core.config import CONFIG_PATH as _runtime_config_path

            remember_calls_in(_runtime_config_path.parent / "subscription_calls.json")
        except Exception:
            logger.exception("Subscription call count could not be restored")
    await startup_worker_impl()
    await startup_event_impl()
    backup_scheduler = None
    schedulers_started = False
    try:
        if not IS_TEST_MODE:
            from core.disaster_recovery import get_backup_scheduler

            backup_scheduler = get_backup_scheduler()
            if backup_scheduler.start():
                logger.info("Disaster-recovery backup scheduler started.")
            from api.research_factory_service import auto_cycle_startup, pe_scan_startup

            started = auto_cycle_startup()
            if started.get("started"):
                logger.info("Research-cycle auto scheduler enabled.")
            pe_started = pe_scan_startup()
            if pe_started.get("started"):
                logger.info("Continuous Pre-Earnings scanner enabled.")
            try:
                from api.state_service import get_shared_config
                from core.development_factory.watchdog import watchdog_startup

                repair_started = watchdog_startup(get_shared_config())
                if repair_started.get("started"):
                    logger.info("Autonomous repair watchdog enabled (detect→fix→verify).")
            except Exception:
                logger.exception("Repair watchdog startup failed")
            try:
                from core.strategy_lab.runner import lab_startup

                lab_started = lab_startup(get_shared_config)
                if lab_started.get("started"):
                    logger.info("Strategy Lab scheduler enabled (daily search, backtest, review, paper).")
            except Exception:
                logger.exception("Strategy Lab startup failed")
            try:
                from core.mlflow_server import ensure_mlflow_server

                mlflow_outcome = ensure_mlflow_server(get_shared_config())
                logger.info("MLflow tracking: %s", mlflow_outcome.get("detail"))
            except Exception:
                logger.exception("MLflow auto-start failed")
            try:
                from core.ml.trainer import check_and_train_baseline

                ml_result = check_and_train_baseline(get_shared_config())
                logger.info("ML baseline: %s", ml_result.get("detail") or ml_result.get("status"))
            except Exception:
                logger.exception("ML baseline training failed")
            try:
                from core.rdagent_worker import ensure_rdagent_worker

                rdagent_result = ensure_rdagent_worker(get_shared_config())
                if rdagent_result.get("enabled"):
                    logger.info("RD-Agent worker: %s", rdagent_result.get("detail"))
            except Exception:
                logger.exception("RD-Agent worker startup failed")
            try:
                def _read_model_warmup() -> None:
                    # Cockpit first: the Home tab is the first thing the browser
                    # asks for, and a request that arrives before any snapshot
                    # exists has to wait for a full build. Start that build
                    # right after start-up (a cold request joins it instead of
                    # duplicating it), then rebuild once more when the agents
                    # that were still starting have data.
                    time.sleep(3)
                    _kick_cockpit_rebuild()
                    time.sleep(17)
                    _kick_cockpit_rebuild()
                    # Warm the /api/state variants the UI polls first: a true
                    # cold build blocks on the news agent's startup deep fetch
                    # (cache lock held for minutes), so the first browser poll
                    # after a restart must find these caches populated.
                    time.sleep(5)
                    _kick_state_rebuild({
                        "cache_key": "api_state:False:False:False:True::False",
                        "include_flags": set(),
                        "include_news": False,
                        "include_hotlist": False,
                        "hotlist_details": False,
                        "lean": True,
                        "state_live_prices": False,
                        "capital_live_prices": False,
                    })
                    _kick_state_rebuild({
                        "cache_key": "api_state:False:False:False:False:capital:False",
                        "include_flags": {"capital"},
                        "include_news": False,
                        "include_hotlist": False,
                        "hotlist_details": False,
                        "lean": False,
                        "state_live_prices": False,
                        "capital_live_prices": False,
                    })

                threading.Thread(target=_read_model_warmup, name="cockpit-warmup", daemon=True).start()
            except Exception:
                logger.exception("Cockpit warmup kickoff failed")
            schedulers_started = True
        yield
    finally:
        if schedulers_started:
            try:
                from core.mlflow_server import stop_mlflow_server

                stop_mlflow_server()
            except Exception:
                logger.exception("MLflow server shutdown failed")
            try:
                from core.rdagent_worker import stop_rdagent_worker

                stop_rdagent_worker()
            except Exception:
                logger.exception("RD-Agent worker shutdown failed")
            try:
                from core.development_factory.watchdog import shutdown_repair_watchdog

                shutdown_repair_watchdog()
            except Exception:
                logger.exception("Repair watchdog shutdown failed")
            try:
                from api.research_factory_service import auto_cycle_shutdown
                auto_cycle_shutdown()
            except Exception:
                logger.exception("Research-cycle scheduler shutdown failed")
            try:
                from api.research_factory_service import pe_scan_shutdown
                pe_scan_shutdown()
            except Exception:
                logger.exception("Continuous Pre-Earnings scanner shutdown failed")
        if backup_scheduler is not None:
            backup_scheduler.stop()
        await shutdown_worker_impl()
        await shutdown_event_impl()


def _ensure_ollama_running() -> None:
    global _OLLAMA_START_ATTEMPTED

    if _ollama_is_running():
        logger.info("Ollama already running.")
        return

    if "ollama.com" in _get_ollama_base_url().lower():
        logger.info("Ollama Cloud configured but not reachable; skipping local Ollama auto-start.")
        return

    with _OLLAMA_START_LOCK:
        if _OLLAMA_START_ATTEMPTED:
            return
        _OLLAMA_START_ATTEMPTED = True

    logger.info("Ollama not reachable. Attempting to start...")
    if not _start_ollama_process():
        return

    # Wait briefly for readiness
    for _ in range(10):
        if _ollama_is_running(timeout_sec=1.0):
            logger.info("Ollama started successfully.")
            return
        time.sleep(0.5)

    logger.warning("Ollama did not respond after start attempt.")

# Initialize FastAPI app
app = FastAPI(
    title="Market Signals V9 - Web API",
    description="API for market signals monitoring and analysis",
    version="9.0.0",
    lifespan=lifespan,
    docs_url=None if _BOOT_IS_PRODUCTION else "/docs",
    redoc_url=None if _BOOT_IS_PRODUCTION else "/redoc",
    openapi_url=None if _BOOT_IS_PRODUCTION else "/openapi.json",
)


def _is_cache_guard_active(path: str) -> bool:
    normalized = str(path or "").split("?", 1)[0]
    if normalized not in _CACHE_GUARD_ENDPOINTS:
        return False

    now_ts = time.time()
    cutoff = now_ts - float(_CACHE_GUARD_WINDOW_SEC)
    with _CACHE_GUARD_LOCK:
        bucket = _CACHE_GUARD_HITS.setdefault(normalized, [])
        bucket.append(now_ts)
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        return len(bucket) >= int(_CACHE_GUARD_THRESHOLD)


def _is_ui_cache_only_path(path: str) -> bool:
    normalized = str(path or "").split("?", 1)[0]
    return normalized in _UI_CACHE_ONLY_ENDPOINTS


def _set_ctx_cache_reason(reason: str) -> None:
    ctx = get_request_context()
    if ctx is not None and isinstance(ctx.metadata, dict) and reason:
        ctx.metadata["cache_reason"] = str(reason)


def _load_json_file_payload(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.debug(f"JSON fallback load failed for {path}: {e}")
    return None


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Real) and not isinstance(value, bool):
        if isinstance(value, int):
            return int(value)
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except Exception:
            return None
    if isinstance(value, dict):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(v) for v in value]
    return value


def _json_payload_bytes(data: Any) -> bytes:
    return json.dumps(
        _json_safe_value(data),
        ensure_ascii=False,
        default=str,
        allow_nan=False,
    ).encode("utf-8")


def _normalize_json_payload_bytes(payload: bytes) -> bytes:
    try:
        raw = payload.decode("utf-8", errors="ignore") if isinstance(payload, bytes) else str(payload)
        return _json_payload_bytes(json.loads(raw))
    except Exception:
        return payload


def _kickoff_premarket_agent_warm() -> None:
    def _worker():
        try:
            from agents.premarket_scanner_agent import get_premarket_scanner_agent

            agent = get_premarket_scanner_agent()
            if _agent_enabled("premarket_scanner") and hasattr(agent, "start"):
                agent.start()
        except Exception as e:
            logger.debug(f"Premarket warm kickoff skipped: {e}")

    try:
        threading.Thread(target=_worker, daemon=True, name="PremarketWarmInit").start()
    except Exception:
        pass

# ==================== API KEY AUTHENTICATION ====================
def _env_flag(name: str, default: bool = False) -> bool:
    value = str(os.getenv(name, "") or "").strip().lower()
    if not value:
        return bool(default)
    return value in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: List[str]) -> List[str]:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return list(default)
    return list(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))


_DEPLOY_ENV = _BOOT_DEPLOY_ENV
_IS_PRODUCTION = _BOOT_IS_PRODUCTION
_FORCE_HTTPS = _env_flag("MS_FORCE_HTTPS", False)
_API_KEY = os.getenv("MS_API_KEY", "").strip()
_METRICS_BEARER_TOKEN = os.getenv("MS_METRICS_BEARER_TOKEN", "").strip()
_AUTH_ENABLED = bool(_API_KEY)
_PROTECT_READS = _env_flag("MS_PROTECT_READS", _IS_PRODUCTION)
_UNPROTECTED_METHODS = {"GET", "HEAD", "OPTIONS"}
_AUTH_PUBLIC_PATHS = {
    "/api/auth/status",
    "/api/auth/login",
    "/api/health",
    "/docs",
    "/openapi.json",
    "/redoc",
}
_SESSION_COOKIE_NAME = (
    "__Secure-ms_operator_session"
    if _IS_PRODUCTION
    else "ms_operator_session"
)
try:
    _SESSION_TTL_SECONDS = max(
        300,
        min(86_400, int(os.getenv("MS_AUTH_SESSION_TTL_SECONDS", "28800") or 28800)),
    )
except (TypeError, ValueError):
    _SESSION_TTL_SECONDS = 28_800

if _AUTH_ENABLED:
    logger.info(
        "Operator authentication ENABLED "
        f"(protected_reads={_PROTECT_READS}, signed_browser_sessions=true)."
    )
else:
    logger.info("API key authentication DISABLED (MS_API_KEY not set). All endpoints are public.")


def _is_cross_site_write(request: Request, method: str) -> bool:
    """True when another website is trying to change state through the browser.

    Without an API key every local endpoint is open, and a body-less POST from
    any web page is sent without a CORS preflight. Browsers label such requests
    with Sec-Fetch-Site / Origin; non-browser clients send neither.
    """
    if method in {"GET", "HEAD", "OPTIONS"}:
        return False
    if str(request.headers.get("sec-fetch-site") or "").lower() == "cross-site":
        return True
    origin = str(request.headers.get("origin") or "").strip()
    if not origin or origin in _allowed_origins:
        return False
    origin_host = origin.split("://", 1)[-1].lower()
    return origin_host != str(request.headers.get("host") or "").lower()


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    """Require a key or signed operator session for protected API operations."""
    method = request.method.upper()
    path = request.url.path
    if _is_cross_site_write(request, method):
        return JSONResponse(
            status_code=403,
            headers={"Cache-Control": "no-store"},
            content={"detail": "Cross-site requests cannot change state."},
        )
    if path == "/api/metrics" and _METRICS_BEARER_TOKEN:
        authorization = str(request.headers.get("authorization") or "")
        scheme, _, credentials = authorization.partition(" ")
        if (
            scheme.lower() == "bearer"
            and credentials
            and hmac.compare_digest(credentials.strip(), _METRICS_BEARER_TOKEN)
        ):
            request.state.operator_auth = "metrics_scraper"
            return await call_next(request)
        if not _AUTH_ENABLED:
            request.state.operator_auth = "metrics_authentication_rejected"
            return JSONResponse(
                status_code=401,
                headers={"Cache-Control": "no-store"},
                content={"detail": "Invalid or missing metrics bearer token."},
            )

    # Skip auth entirely if no key configured (development mode)
    if not _AUTH_ENABLED:
        request.state.operator_auth = "development_unauthenticated"
        return await call_next(request)

    if path in _AUTH_PUBLIC_PATHS:
        request.state.operator_auth = "public_endpoint"
        return await call_next(request)

    protected_read = bool(
        _PROTECT_READS
        and method in _UNPROTECTED_METHODS
        and path.startswith("/api/")
    )
    protected_write = method not in _UNPROTECTED_METHODS
    if not protected_read and not protected_write:
        request.state.operator_auth = "public_asset"
        return await call_next(request)

    # Query-string credentials leak to logs and proxies. Keep them as a
    # development-only compatibility path; production accepts headers only.
    provided_key = request.headers.get("x-api-key")
    if not provided_key and not _IS_PRODUCTION:
        provided_key = request.query_params.get("api_key")

    if provided_key and hmac.compare_digest(str(provided_key), _API_KEY):
        request.state.operator_auth = "api_key"
        return await call_next(request)

    session = _decode_operator_session(request.cookies.get(_SESSION_COOKIE_NAME))
    if session is not None:
        if protected_write:
            provided_csrf = request.headers.get("x-csrf-token", "")
            if not provided_csrf or not hmac.compare_digest(
                str(provided_csrf),
                str(session["csrf_token"]),
            ):
                request.state.operator_auth = "browser_session_csrf_rejected"
                return JSONResponse(
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                    content={"detail": "Missing or invalid CSRF token."},
                )
        request.state.operator_auth = "browser_session"
        return await call_next(request)

    # Auth failure — log and return 401
    request.state.operator_auth = "authentication_rejected"
    client_ip = request.client.host if request.client else "unknown"
    logger.warning(
        json.dumps({
            "kind": "auth_failure",
            "method": method,
            "path": path,
            "client_ip": client_ip,
            "has_header": bool(request.headers.get("x-api-key")),
            "has_query_param": bool(request.query_params.get("api_key")),
        })
    )
    return JSONResponse(
        status_code=401,
        headers={"Cache-Control": "no-store"},
        content={"detail": "Invalid or missing API key. Provide X-API-Key header."},
    )


def _encode_operator_session(expires_at: int, csrf_token: str) -> str:
    payload = f"{int(expires_at)}.{csrf_token}"
    signature = hmac.new(
        _API_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def _decode_operator_session(raw_token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not _AUTH_ENABLED or not raw_token:
        return None
    try:
        expires_raw, csrf_token, signature = str(raw_token).split(".", 2)
        expires_at = int(expires_raw)
        if expires_at <= int(time.time()):
            return None
        if expires_at > int(time.time()) + _SESSION_TTL_SECONDS + 60:
            return None
        if len(csrf_token) != 32 or any(ch not in "0123456789abcdef" for ch in csrf_token):
            return None
        payload = f"{expires_at}.{csrf_token}"
        expected = hmac.new(
            _API_KEY.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        return {"expires_at": expires_at, "csrf_token": csrf_token}
    except (TypeError, ValueError):
        return None


class AuthLoginRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


@app.post("/api/auth/login")
async def auth_login(payload: AuthLoginRequest, request: Request):
    """Exchange the operator API key for an HttpOnly, short-lived session."""
    provided = str(payload.api_key or "")
    if not _AUTH_ENABLED or not hmac.compare_digest(provided, _API_KEY):
        request.state.operator_auth = "login_rejected"
        return JSONResponse(
            status_code=401,
            headers={"Cache-Control": "no-store"},
            content={"detail": "Invalid API key."},
        )
    request.state.operator_auth = "api_key_login"
    expires_at = int(time.time()) + _SESSION_TTL_SECONDS
    csrf_token = secrets.token_hex(16)
    response = JSONResponse(
        headers={"Cache-Control": "no-store"},
        content={
            "authenticated": True,
            "expires_at": expires_at,
            "csrf_token": csrf_token,
        }
    )
    response.set_cookie(
        key=_SESSION_COOKIE_NAME,
        value=_encode_operator_session(expires_at, csrf_token),
        max_age=_SESSION_TTL_SECONDS,
        expires=_SESSION_TTL_SECONDS,
        path="/api",
        secure=bool(_IS_PRODUCTION or _FORCE_HTTPS),
        httponly=True,
        samesite="strict",
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout():
    response = JSONResponse(
        headers={"Cache-Control": "no-store"},
        content={"authenticated": False},
    )
    response.delete_cookie(
        key=_SESSION_COOKIE_NAME,
        path="/api",
        secure=bool(_IS_PRODUCTION or _FORCE_HTTPS),
        httponly=True,
        samesite="strict",
    )
    return response


@app.get("/api/auth/status")
async def auth_status(request: Request, response: Response):
    """Return auth policy and current browser-session state without secrets."""
    session = _decode_operator_session(request.cookies.get(_SESSION_COOKIE_NAME))
    response.headers["Cache-Control"] = "no-store"
    return {
        "auth_enabled": _AUTH_ENABLED,
        "key_configured": bool(_API_KEY),
        "protect_reads": bool(_PROTECT_READS),
        "authenticated": bool(session),
        "session_expires_at": session.get("expires_at") if session else None,
        "csrf_token": session.get("csrf_token") if session else None,
    }


# ==================== MIDDLEWARE: REQUEST-ID + TIMING ====================
@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    # Propagate only a bounded safe request-id; otherwise generate one.
    request_id = ensure_scope_request_id(request.scope)
    start = time.perf_counter()
    path = request.url.path
    is_ui_read = request.method.upper() == "GET" and path.startswith("/api/")
    include_param = str(request.query_params.get("include") or "").strip().lower()
    live_prices_param = str(request.query_params.get("live_prices") or "").strip().lower()
    live_capital_request = bool(
        path == "/api/state"
        and "capital" in {part.strip() for part in include_param.split(",") if part.strip()}
        and live_prices_param not in {"0", "false", "no", "off"}
    )
    strict_cache_only = bool(is_ui_read and _is_ui_cache_only_path(path) and not live_capital_request)
    guard_cache_only = bool(is_ui_read and _is_cache_guard_active(path) and not live_capital_request)
    cache_only = bool(strict_cache_only or guard_cache_only)
    reason = "UI_READ_CACHE_ONLY" if cache_only else ("UI_READ" if is_ui_read else "UI_WRITE")
    ctx = RequestContext(
        reason=reason,
        path=path,
        cache_only=cache_only,
        metadata={
            "method": request.method.upper(),
            "cache_only_strict": strict_cache_only,
            "cache_only_guard": guard_cache_only,
            "request_id": request_id,
        },
    )
    token = set_request_context(ctx)
    response = None
    try:
        response = await call_next(request)
        if cache_only:
            response.headers["x-cache-guard"] = "ACTIVE"
            response.headers["x-cache-policy"] = "CACHE_ONLY"
        # Always echo request_id so client can correlate to logs.
        response.headers["x-request-id"] = request_id
        return response
    finally:
        duration_ms = (time.perf_counter() - start) * 1000.0
        try:
            status_code = getattr(response, "status_code", 200)
        except Exception:
            status_code = 500
        route = request.scope.get("route")
        route_path = str(getattr(route, "path", "") or "")
        metric_path = (
            route_path
            if route_path
            else "/api/unrouted"
            if path.startswith("/api/")
            else "/unrouted"
        )
        record_internal_request(
            path=metric_path,
            status=status_code,
            duration_ms=duration_ms,
        )
        ctx_now = get_request_context()
        if response is not None:
            cache_reason = None
            req_external_calls = 0
            if ctx_now and isinstance(ctx_now.metadata, dict):
                cache_reason = ctx_now.metadata.get("cache_reason")
                req_external_calls = int(ctx_now.metadata.get("external_calls", 0) or 0)
            if not cache_reason and is_ui_read:
                cache_reason = "MISS"
            if cache_reason:
                response.headers["x-cache"] = str(cache_reason)
            # Provider calls attributed to THIS request's handler (excludes background
            # daemon threads). Lets cache-only/budget assertions stay deterministic.
            response.headers["x-request-external-calls"] = str(req_external_calls)
            # Server-side handling duration (ms). Exposes true endpoint performance
            # independent of client/harness overhead (e.g. TestClient threadpool
            # queueing under concurrent bursts), so perf guardrails can assert on
            # endpoint cost rather than wall-clock client latency.
            response.headers["x-duration-ms"] = str(round(duration_ms, 2))
        # Structured JSON access log — one line per request.
        log_entry = {
            "kind": "access",
            "request_id": request_id,
            "method": request.method.upper(),
            "path": path,
            "status": status_code,
            "duration_ms": round(duration_ms, 2),
            "cache_only": cache_only,
        }
        if duration_ms > _SLOW_REQUEST_MS:
            logger.warning(json.dumps(log_entry, ensure_ascii=True))
        else:
            logger.info(json.dumps(log_entry, ensure_ascii=True))
        clear_request_context(token)

# Security headers are intentionally compatible with the current single-page UI.
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if _FORCE_HTTPS:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


_default_origins = ["http://localhost:8003", "http://localhost:8000", "http://127.0.0.1:8003"]
_allowed_origins = _env_csv("MS_ALLOWED_ORIGINS", [] if _IS_PRODUCTION else _default_origins)
_default_hosts = ["localhost", "127.0.0.1", "testserver"]
_trusted_hosts = _env_csv("MS_TRUSTED_HOSTS", _default_hosts)

if _FORCE_HTTPS:
    app.add_middleware(HTTPSRedirectMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_trusted_hosts)

# Same-origin frontend requests do not require CORS. Production origins must be
# intentionally listed through MS_ALLOWED_ORIGINS; no wildcard is accepted.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=bool(_allowed_origins),
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-API-Key",
        "X-CSRF-Token",
        "X-Request-ID",
    ],
)

# ==================== Bounded API Traffic Protection ====================
_traffic_controller = get_api_traffic_controller()
_TRAFFIC_CONFIG = _traffic_controller.config
_RATE_LIMIT_WINDOW = _TRAFFIC_CONFIG.window_seconds
_RATE_LIMIT_MAX_READS = _TRAFFIC_CONFIG.read_requests
_RATE_LIMIT_MAX_WRITES = _TRAFFIC_CONFIG.write_requests
_RATE_LIMIT_MAX_AUTH = _TRAFFIC_CONFIG.auth_requests
_RATE_LIMIT_MAX_BUCKETS = _TRAFFIC_CONFIG.max_buckets
# Compatibility alias used by existing test isolation and load probes.
_rate_limit_store = _traffic_controller.buckets


def _current_rate_limits() -> Dict[str, int]:
    return {
        "read": _RATE_LIMIT_MAX_READS,
        "write": _RATE_LIMIT_MAX_WRITES,
        "auth": _RATE_LIMIT_MAX_AUTH,
    }


app.add_middleware(
    ApiTrafficProtectionMiddleware,
    controller=_traffic_controller,
    rate_limits_provider=_current_rate_limits,
)

# GZip for JSON read models: /api/state alone ships ~2.4 MB per capital poll
# and the audit measured NO compression on any endpoint (Response(bytes)
# paths included — GZipMiddleware compresses those too). Localhost-heavy UI
# polls drop ~10x on the wire for a small CPU cost (audit 2026-09-05, P2).
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def operator_audit_middleware(request: Request, call_next):
    """Fail closed and durably audit every API mutation without payload data."""
    method = request.method.upper()
    path = request.url.path
    audited = bool(
        path.startswith("/api/")
        and method in {"POST", "PUT", "PATCH", "DELETE"}
    )
    if not audited:
        return await call_next(request)

    from core.operator_audit import get_operator_audit_ledger

    ledger = get_operator_audit_ledger()
    if not ledger.enabled:
        return await call_next(request)

    request_id = ensure_scope_request_id(request.scope)
    ready = await asyncio.to_thread(ledger.ensure_ready)
    if not ready:
        logger.critical(
            "Operator audit unavailable; refusing mutation %s %s",
            method,
            path,
        )
        return JSONResponse(
            status_code=503,
            headers={
                "Cache-Control": "no-store",
                "X-Request-ID": request_id,
            },
            content={
                "detail": "Operator audit unavailable; mutation refused.",
                "request_id": request_id,
            },
        )

    request.state.operator_auth = "not_authenticated"
    started = time.perf_counter()
    response = None
    try:
        response = await call_next(request)
    finally:
        duration_ms = (time.perf_counter() - started) * 1000.0
        status_code = int(getattr(response, "status_code", 500) or 500)
        response_request_id = (
            response.headers.get("x-request-id")
            if response is not None
            else None
        )
        actor = str(
            getattr(request.state, "operator_auth", "unknown")
            or "unknown"
        )
        client_address = request.client.host if request.client else "unknown"
        try:
            await asyncio.to_thread(
                ledger.append_http_event,
                request_id=response_request_id or request_id,
                method=method,
                path=path,
                status_code=status_code,
                duration_ms=duration_ms,
                actor=actor,
                client_address=client_address,
            )
        except Exception as exc:
            logger.critical(
                "Operator mutation outcome is uncertain because audit append failed: %s",
                exc,
                exc_info=True,
            )
            response = JSONResponse(
                status_code=503,
                headers={
                    "Cache-Control": "no-store",
                    "X-Request-ID": response_request_id or request_id,
                },
                content={
                    "detail": "Audit persistence failed; mutation outcome is uncertain.",
                    "request_id": response_request_id or request_id,
                },
            )
    return response

# ==================== Pydantic Models ====================

class TickerRequest(BaseModel):
    ticker: str
    return_state: Optional[bool] = True
    # Add ticker only: add the symbol even when no quote was found for it.
    force: Optional[bool] = False

class TickersDeleteRequest(BaseModel):
    tickers: List[str]
    return_state: Optional[bool] = False

class CapitalResetRequest(BaseModel):
    amount: float
    reason: Optional[str] = "Manual Reset"
    clear_positions: Optional[bool] = True


class CapitalOpenPositionRequest(BaseModel):
    ticker: str
    direction: str
    leverage: Optional[int] = 1
    usd_size: float
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    reason: Optional[str] = "Manual terminal order"
    strategy_type: Optional[str] = "MANUAL"


class CapitalClosePositionRequest(BaseModel):
    position_id: str
    exit_price: Optional[float] = None
    reason: Optional[str] = "Manual close"


class CapitalUpdatePositionLevelsRequest(BaseModel):
    position_id: str
    stop_loss_price: float
    take_profit_price: float
    reason: Optional[str] = "Manual SL/TP update"


class ConfigUpdateRequest(BaseModel):
    config: dict


class AIAssignmentRequest(BaseModel):
    assignment: dict = Field(default_factory=dict)

class AIRouterRequest(BaseModel):
    router: dict = Field(default_factory=dict)

class AIValidatedRequest(BaseModel):
    provider: str
    prompt: str
    model: Optional[str] = None
    system: str = ""


class BackupCreateRequest(BaseModel):
    label: Optional[str] = Field(default="manual-api", max_length=40)

class TickersReorderRequest(BaseModel):
    tickers: List[str]

class BacktestRequest(BaseModel):
    days: Optional[int] = 30
    risk_mode: Optional[str] = "MEDIUM"
    trade_mode: Optional[str] = "PULLBACK"
    tickers: Optional[List[str]] = None  # If None, use all watchlist
    compare_all: Optional[bool] = False
    orb_entry_type: Optional[str] = "MIDPOINT"  # For ORB_FVG: MIDPOINT, CONSERVATIVE, or BOTH
    custom_tp_pct: Optional[float] = None
    custom_sl_pct: Optional[float] = None
    variant_mode: Optional[str] = "STRICT"

class WalkForwardRequest(BaseModel):
    ticker: str
    days: Optional[int] = 60
    train_days: Optional[int] = 20
    test_days: Optional[int] = 10
    risk_modes: Optional[List[str]] = None
    trade_modes: Optional[List[str]] = None
    custom_tp_pct: Optional[float] = None
    custom_sl_pct: Optional[float] = None
    max_windows: Optional[int] = 6
    variant_mode: Optional[str] = "STRICT"

class AnalyzeRequest(BaseModel):
    ticker: str
    include_heavy: Optional[bool] = False
    disable_ollama: Optional[bool] = False
    force_refresh: Optional[bool] = False
    mode: Optional[str] = None


class StrategyConfigRequest(BaseModel):
    config: dict


class QuickAIRequest(BaseModel):
    ticker: str
    force_refresh: Optional[bool] = False


class AlertRequest(BaseModel):
    ticker: str
    type: str # PRICE_UP, PRICE_DOWN, VOLATILITY
    target: float

class AlertToggleRequest(BaseModel):
    alert_id: str


class TradeRadarRefreshRequest(BaseModel):
    force: Optional[bool] = False
    symbols: Optional[List[str]] = None


class TradeRadarConfigRequest(BaseModel):
    threshold_pct: Optional[float] = None
    radar_interval_sec: Optional[int] = None
    cooldown_sec: Optional[int] = None
    max_candidates: Optional[int] = None
    max_per_cycle: Optional[int] = None
    backtest_days: Optional[int] = None
    enable_backtest: Optional[bool] = None
    ohlc_ttl_sec: Optional[int] = None
    news_ttl_sec: Optional[int] = None
    calendar_ttl_sec: Optional[int] = None
    backtest_ttl_sec: Optional[int] = None
    local_only_mode: Optional[bool] = None
    allow_remote_on_miss: Optional[bool] = None
    allow_analysis_remote: Optional[bool] = None
    allow_ohlc_remote: Optional[bool] = None
    allow_news_refresh: Optional[bool] = None
    allow_calendar_refresh: Optional[bool] = None


class TradeRadarSuppressRequest(BaseModel):
    symbols: List[str]
    duration_sec: Optional[int] = None


_PRE_EARNINGS_COVERAGE_SYMBOLS_DEFAULT_LIMIT = 5
_PRE_EARNINGS_COVERAGE_SYMBOLS_HARD_LIMIT = 50
_PRE_EARNINGS_SYMBOL_RE = re.compile(r"[A-Z]{1,6}(?:\.[A-Z])?")


def _normalize_pre_earnings_symbol(raw_symbol: Any) -> str:
    symbol = str(raw_symbol or "").upper().strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol required")
    if len(symbol) > 8 or _PRE_EARNINGS_SYMBOL_RE.fullmatch(symbol) is None:
        raise HTTPException(status_code=400, detail="invalid symbol")
    return symbol


class PreEarningsCoverageWarmupRequest(BaseModel):
    symbols: Optional[List[str]] = Field(
        default=None,
        max_length=_PRE_EARNINGS_COVERAGE_SYMBOLS_HARD_LIMIT,
    )
    only_needs_coverage: Optional[bool] = True
    batch_size: Optional[int] = 5
    allow_remote_on_miss: Optional[bool] = True


def _normalize_direction(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in {"buy", "long"}:
        return "long"
    if value in {"sell", "short"}:
        return "short"
    return ""


def _get_cached_entry_price(ticker: str) -> Optional[float]:
    symbol = str(normalize_market_ticker(ticker) or "").upper().strip()
    if not symbol:
        return None
    try:
        from core.ticker_workspace import get_ticker_workspace_store

        ws_market = get_ticker_workspace_store().get_market_data(symbol) or {}
        price = float(ws_market.get("price", 0.0) or 0.0)
        if price > 0:
            return price
    except Exception:
        pass
    try:
        fresh_state = get_state(include_news=False, include_hotlist=False, lean=True) or {}
        rows = fresh_state.get("tickers", []) if isinstance(fresh_state, dict) else []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_ticker = str(normalize_market_ticker(row.get("ticker")) or "").upper().strip()
                if row_ticker != symbol:
                    continue
                price = float(row.get("price", 0.0) or 0.0)
                if price > 0:
                    return price
                break
    except Exception:
        pass
    try:
        state = load_state_payload({}) or {}
        rows = state.get("tickers", {}) if isinstance(state, dict) else {}
        if isinstance(rows, dict):
            for raw_key, node in rows.items():
                raw_symbol = str(normalize_market_ticker(raw_key) or "").upper().strip()
                if raw_symbol != symbol or not isinstance(node, dict):
                    continue
                price = float(node.get("price", 0.0) or 0.0)
                if price > 0:
                    return price
                break
    except Exception:
        pass
    try:
        from api.market_data_service import get_ticker_data

        live_data = get_ticker_data(symbol, lightweight=True) or {}
        price = float(live_data.get("price", 0.0) or 0.0)
        if price > 0:
            return price
    except Exception:
        pass
    return None


def _is_verified_execution_price(exec_data: Optional[dict]) -> bool:
    if not isinstance(exec_data, dict):
        return False
    try:
        price = float(exec_data.get("execution_price", 0.0) or 0.0)
    except Exception:
        return False
    if price <= 0:
        return False

    source = str(exec_data.get("source") or "").strip().lower()
    if source == "fallback":
        return False

    quality_meta = exec_data.get("execution_price_quality") or {}
    if not isinstance(quality_meta, dict):
        return False
    level = str(quality_meta.get("level") or quality_meta.get("quality") or "").strip().upper()
    if quality_meta.get("is_valid") is False:
        return False
    return level == "VERIFIED"


def _agent_enabled(agent_key: str, cfg: Optional[dict] = None, default: bool = True) -> bool:
    return is_agent_enabled(agent_key, config=cfg, default=default)


def _apply_runtime_agent_switches(prev_cfg: dict, new_cfg: dict) -> None:
    prev_agents = (prev_cfg or {}).get("agents", {}) or {}
    new_agents = (new_cfg or {}).get("agents", {}) or {}

    def _flag(name: str, agents_cfg: dict, default: bool = True) -> bool:
        value = agents_cfg.get(name, {})
        if isinstance(value, dict):
            return bool(value.get("enabled", default))
        if isinstance(value, bool):
            return bool(value)
        return bool(default)

    defaults = {"premarket_scanner": False, "pre_earnings": False}

    for name in (
        "trade_radar",
        "strategy_research",
        "premarket_scanner",
        "pre_earnings",
        "news_tab",
        "economic_calendar",
    ):
        default_enabled = bool(defaults.get(name, True))
        before = _flag(name, prev_agents, default_enabled)
        after = _flag(name, new_agents, default_enabled)
        if before == after:
            continue
        try:
            runtime = set_agent_runtime(
                name,
                after,
                config=new_cfg,
                engine_running=is_engine_running,
                start_engine=start_engine,
                test_mode=IS_TEST_MODE,
            )
            update_agent_status(name, **runtime)
        except Exception as e:
            logger.error(f"Failed to apply runtime switch for agent '{name}': {e}")

# ==================== API Endpoints ====================

@app.get("/api/alerts")
def api_get_alerts():
    """Get all active alerts"""
    try:
        alerts = get_alerts()
        return alerts
    except Exception as e:
        logger.error(f"Error getting alerts: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/alerts")
def api_create_alert(request: AlertRequest):
    """Create a new alert"""
    try:
        result = create_alert(request.ticker, request.type, request.target)
        if "error" in result:
             raise HTTPException(status_code=result.get("status_code", 500), detail=result["error"])
        return {"status": "ok", "alert": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating alert: {e}", exc_info=True)
        raise _safe_error()

@app.delete("/api/alerts/{alert_id}")
def api_delete_alert(alert_id: str):
    """Delete an alert"""
    try:
        result = delete_alert(alert_id)
        if result.get("success"):
            return {"status": "ok", "deleted": alert_id}
        else:
            raise HTTPException(status_code=404, detail=result.get("error", "Alert not found"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting alert: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/alerts/toggle")
def api_toggle_alert(request: AlertToggleRequest):
    """Toggle an alert's active status"""
    try:
        result = toggle_alert(request.alert_id)
        if result.get("success"):
            return {"status": "ok", "alert_id": request.alert_id, "active": result.get("active")}
        else:
            raise HTTPException(status_code=404, detail=result.get("error", "Alert not found"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling alert: {e}", exc_info=True)
        raise _safe_error()


class TelegramSettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    bot_token: Optional[str] = None  # empty string = keep stored token
    chat_id: Optional[str] = None
    routes: Optional[Dict[str, bool]] = None  # news/signals/capital/alerts


@app.get("/api/alerts/telegram")
def api_get_telegram_settings():
    """Telegram delivery settings for the Alerts tab (token never returned raw)."""
    try:
        from api.state_service import get_shared_config
        from core.telegram_service import get_telegram_settings

        return get_telegram_settings(get_shared_config())
    except Exception as e:
        logger.error(f"Error reading telegram settings: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/alerts/telegram")
def api_save_telegram_settings(request: TelegramSettingsRequest):
    """Persist Telegram delivery settings selected in the Alerts tab."""
    try:
        from api.state_service import get_shared_config
        from core.telegram_service import save_telegram_settings

        settings = save_telegram_settings(
            enabled=request.enabled,
            bot_token=request.bot_token,
            chat_id=request.chat_id,
            routes=request.routes,
            config=get_shared_config(),
        )
        return {"status": "ok", "settings": settings}
    except ValueError as e:
        # Malformed token / chat id (e.g. the masked placeholder pasted back):
        # nothing was written, tell the owner why instead of a generic 500.
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error saving telegram settings: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/alerts/telegram/test")
def api_test_telegram():
    """Send one test message using the stored settings."""
    try:
        from core.telegram_service import test_telegram

        ok, detail = test_telegram()
        return {"status": "ok" if ok else "error", "detail": detail}
    except Exception as e:
        logger.error(f"Error testing telegram: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/capital/reset")
def api_reset_capital(request: CapitalResetRequest):
    """Reset capital balance"""
    try:
        from core.capital_manager import get_capital_manager
        amount = float(request.amount or 0.0)
        if not math.isfinite(amount) or amount <= 0:
            raise HTTPException(status_code=400, detail="amount must be > 0")
        cm = get_capital_manager()
        cm.set_balance(amount, request.reason, clear_positions=bool(request.clear_positions))
        _invalidate_capital_state_cache()
        return {
            "status": "ok",
            "balance": cm.get_deployable_balance(),
            "positions": cm.get_positions(),
            "history": cm.get_history(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resetting capital: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/capital/open-position")
def api_open_capital_position(request: CapitalOpenPositionRequest):
    """Open a manual position with leverage, using cached market price only."""
    try:
        from core.capital_manager import get_capital_manager
        from core.market_hours import MarketHoursError, assert_trade_allowed

        ticker = str(normalize_market_ticker(request.ticker) or "").upper().strip()
        direction = _normalize_direction(request.direction)
        if not ticker:
            raise HTTPException(status_code=400, detail="ticker is required")
        if direction not in {"long", "short"}:
            raise HTTPException(status_code=400, detail="direction must be long|short")
        # Manual opens bypass the regular market-hours guard (user trades 24/7).
        # Non-manual strategy_types reaching this endpoint still enforce hours.
        strategy_type_raw = str(request.strategy_type or "MANUAL").upper().strip()
        if not strategy_type_raw.startswith("MANUAL"):
            try:
                assert_trade_allowed(ticker, "open")
            except MarketHoursError as exc:
                raise HTTPException(status_code=409, detail=str(exc))

        cfg = get_config() or {}
        capital_cfg = (cfg.get("capital") or {}) if isinstance(cfg, dict) else {}
        max_leverage = max(1, min(5, int(capital_cfg.get("max_manual_leverage", 5) or 5)))
        leverage = int(request.leverage or 1)
        if leverage < 1 or leverage > max_leverage:
            raise HTTPException(status_code=400, detail=f"leverage must be in [1,{max_leverage}]")

        usd_size = float(request.usd_size or 0.0)
        if not math.isfinite(usd_size) or usd_size <= 0:
            raise HTTPException(status_code=400, detail="usd_size must be > 0")

        max_size_pct = float(capital_cfg.get("max_manual_position_size_pct", 0.4) or 0.4)
        max_size_pct = max(0.01, min(1.0, max_size_pct))
        default_sl_pct = float(capital_cfg.get("default_manual_stop_loss_pct", 0.02) or 0.02)
        default_tp_pct = float(capital_cfg.get("default_manual_take_profit_pct", 0.04) or 0.04)

        # Use live execution price; order entry must not use cached mid prices.
        cm = get_capital_manager()
        exec_data = cm._get_execution_price(ticker, "BUY" if direction == "long" else "SELL", use_cache=False)
        entry_price = float(exec_data.get("execution_price", 0.0) or 0.0)
        if not _is_verified_execution_price(exec_data):
            raise HTTPException(status_code=409, detail=f"verified live entry price unavailable for {ticker}")
        commission_rate = float(exec_data.get("commission_rate", 0.0) or 0.0)
        if commission_rate < 0:
            raise HTTPException(status_code=409, detail=f"valid configured commission unavailable for {ticker}")
        fees_bps = commission_rate * 10_000.0
        cost_model = exec_data.get("cost_model") if isinstance(exec_data.get("cost_model"), dict) else {}
        if not cost_model:
            # Execution quotes created by older adapters may include the rate
            # but not the model metadata.  Preserve that explicit rate rather
            # than falling back to the legacy capital.fees_bps setting.
            cost_model = {
                "commission_bps_per_side": fees_bps,
                "provenance": "execution_quote_estimate",
                "is_realized_exchange_fee": False,
                "reconciliation_required": "Account fee tier, actual fills and perpetual funding are not included.",
            }

        raw_sl_pct = request.stop_loss_pct if request.stop_loss_pct is not None else default_sl_pct
        raw_tp_pct = request.take_profit_pct if request.take_profit_pct is not None else default_tp_pct
        stop_loss_pct = max(0.001, float(raw_sl_pct or default_sl_pct))
        take_profit_pct = max(0.001, float(raw_tp_pct or default_tp_pct))
        stop_loss_price = entry_price * (1 - stop_loss_pct) if direction == "long" else entry_price * (1 + stop_loss_pct)
        take_profit_price = entry_price * (1 + take_profit_pct) if direction == "long" else entry_price * (1 - take_profit_pct)

        from core.trade_decision import build_manual_decision_context

        decision_context = build_manual_decision_context(
            ticker=ticker,
            direction=direction,
            entry_price=entry_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            usd_size=usd_size,
            leverage=leverage,
            reason=str(request.reason or "Manual terminal order"),
            strategy_type=str(request.strategy_type or "MANUAL"),
        )
        result = cm.open_manual_position(
            ticker=ticker,
            direction=direction,
            leverage=leverage,
            usd_size=usd_size,
            entry_price=entry_price,
            max_size_pct=max_size_pct,
            fees_bps=fees_bps,
            cost_model=cost_model,
            execution_data=exec_data,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            decision_context=decision_context,
            reason=str(request.reason or "Manual terminal order"),
            strategy_type=str(request.strategy_type or "MANUAL"),
        )
        _invalidate_capital_state_cache()
        return {
            "status": "ok",
            "position": result,
            "balance": cm.get_deployable_balance(),
            "positions": cm.get_positions(),
            "history": cm.get_history(),
        }
    except HTTPException:
        raise
    except MarketHoursError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e) or "Invalid request")
    except Exception as e:
        logger.error(f"Error opening manual capital position: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/capital/close-position")
def api_close_capital_position(request: CapitalClosePositionRequest):
    """Close an existing manual position and realize PnL."""
    try:
        from core.capital_manager import get_capital_manager
        from core.market_hours import MarketHoursError, assert_trade_allowed

        cm = get_capital_manager()
        exit_price = request.exit_price
        pos = cm.get_position(str(request.position_id or ""))
        if not pos:
            raise HTTPException(status_code=404, detail="position not found")

        entry_price = float(pos.get("entry_price") or 0.0)
        direction = str(pos.get("direction", "LONG")).upper()
        ticker = pos.get("ticker", "")
        try:
            assert_trade_allowed(ticker, "close")
        except MarketHoursError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        exec_data = None
        if exit_price is None or float(exit_price or 0.0) <= 0:
            # No manual exit price → fetch live price
            closing_side = "SELL" if direction == "LONG" else "BUY"
            exec_data = cm._get_execution_price(ticker, closing_side, use_cache=False)
            live_price = float(exec_data.get("execution_price", 0.0) or 0.0)
            if not _is_verified_execution_price(exec_data):
                raise HTTPException(status_code=409, detail=f"verified live exit price unavailable for {ticker}")
            exit_price = live_price
        else:
            # Manual exit price → sanity check against entry to prevent fat-finger errors
            if not math.isfinite(float(exit_price)):
                raise HTTPException(status_code=400, detail="exit_price must be a finite number")
            if entry_price > 0:
                deviation = abs(float(exit_price) - entry_price) / entry_price
                if deviation > 0.15:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Exit price {exit_price} deviates {deviation*100:.1f}% from entry {entry_price}. Use live price or confirm within ±15%."
                    )
        result = cm.close_position(
            position_id=str(request.position_id or ""),
            exit_price=float(exit_price),
            reason=str(request.reason or "Manual close"),
            exit_execution_data=exec_data,
        )
        _invalidate_capital_state_cache()
        return {
            "status": "ok",
            "position": result,
            "balance": cm.get_deployable_balance(),
            "positions": cm.get_positions(),
            "history": cm.get_history(),
        }
    except HTTPException:
        raise
    except MarketHoursError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e) or "Invalid request")
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'") or "Resource not found")
    except Exception as e:
        logger.error(f"Error closing manual capital position: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/capital/update-position-levels")
def api_update_capital_position_levels(request: CapitalUpdatePositionLevelsRequest):
    """Safely replace SL/TP for an open position without executing a trade."""
    try:
        from core.capital_manager import get_capital_manager

        cm = get_capital_manager()
        position_id = str(request.position_id or "").strip()
        position = cm.get_position(position_id)
        if not position:
            raise HTTPException(status_code=404, detail="position not found")
        if str(position.get("status") or "").upper() != "OPEN":
            raise HTTPException(status_code=409, detail="position already closed")

        direction = _normalize_direction(str(position.get("direction") or ""))
        if direction not in {"long", "short"}:
            raise HTTPException(status_code=400, detail="position direction must be long|short")
        ticker = str(position.get("ticker") or "").upper().strip()
        closing_side = "SELL" if direction == "long" else "BUY"
        quote = cm._get_execution_price(ticker, closing_side, use_cache=False)
        live_price = float(quote.get("execution_price", 0.0) or 0.0)
        if not _is_verified_execution_price(quote):
            raise HTTPException(
                status_code=409,
                detail=f"verified live price unavailable for editing {ticker} protection levels",
            )

        updated = cm.update_position_levels(
            position_id=position_id,
            stop_loss_price=float(request.stop_loss_price),
            take_profit_price=float(request.take_profit_price),
            current_price=live_price,
            reason=str(request.reason or "Manual SL/TP update"),
        )
        _invalidate_capital_state_cache()
        return {
            "status": "ok",
            "position": updated,
            "live_price": live_price,
            "balance": cm.get_deployable_balance(),
            "positions": cm.get_positions(),
            "history": cm.get_history(),
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e) or "Invalid protection levels")
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'") or "Resource not found")
    except Exception as e:
        logger.error(f"Error updating capital position levels: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/capital/decisions")
def api_capital_decisions(limit: int = 30):
    """Return recent audit events plus the bounded prospective News evidence."""
    try:
        safe_limit = max(1, min(100, int(limit or 30)))
        cache_key = f"api_capital_decisions:{safe_limit}"
        cached = _endpoint_payload_get(cache_key, ttl_seconds=3.0)
        if cached is not None:
            return Response(content=cached, media_type="application/json")
        from core.trade_audit import get_trade_audit
        from core.execution_archive import read_recent_executions

        records = get_trade_audit(limit=safe_limit)
        news_records = read_recent_executions(
            limit=safe_limit,
            event_types={"autonomous_news_evaluated"},
        )
        seen_event_ids = {str(record.get("event_id") or "") for record in records if isinstance(record, dict)}
        combined = list(records)
        combined.extend(
            record for record in news_records
            if isinstance(record, dict) and str(record.get("event_id") or "") not in seen_event_ids
        )
        combined.sort(key=lambda record: str(record.get("timestamp") or ""), reverse=True)
        combined = combined[:safe_limit]
        payload = {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "count": len(combined),
            "archive_backed_news_evidence_count": len(news_records),
            "records": combined,
        }
        payload_bytes = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        _endpoint_payload_set(cache_key, payload_bytes)
        return Response(content=payload_bytes, media_type="application/json")
    except Exception as e:
        logger.error(f"Error getting capital decision audit: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/capital/equity-curve")
def api_capital_equity_curve(limit: int = 90):
    """Return daily equity snapshots for the equity curve chart."""
    try:
        from core.capital_manager import get_capital_manager, resolve_capital_equity

        cm = get_capital_manager()
        safe_limit = max(1, min(365, int(limit or 90)))
        curve = cm.get_equity_curve(limit=safe_limit)
        current_balance = cm.get_deployable_balance()
        positions = cm.get_positions()
        open_positions = [
            p for p in positions
            if str(p.get("status", "")).upper() == "OPEN"
            and p.get("research_only") is not True
            and str(p.get("position_type") or "").lower() != "research_cohort"
        ]
        total_margin = sum(float(p.get("margin_used", 0.0) or 0.0) for p in open_positions)
        current_equity = resolve_capital_equity(cm, open_positions)
        mark_to_market_pnl = current_equity - current_balance - total_margin
        current_snapshot = {
            "date": datetime.now(timezone.utc).isoformat(),
            "total_equity": round(current_equity, 2),
            "balance": round(current_balance, 2),
            "deployable_cash": round(current_balance, 2),
            "open_margin": round(total_margin, 2),
            "mark_to_market_pnl": round(mark_to_market_pnl, 2),
            "open_positions_count": len(open_positions),
            "metric": "deployable_capital_equity",
            "pnl_basis": "gross_mark_to_market_before_future_exit_costs",
            "research_excluded": True,
            "is_current": True,
        }
        return {"curve": curve, "current": current_snapshot}
    except Exception as e:
        logger.error(f"Error getting capital equity curve: {e}", exc_info=True)
        raise _safe_error()


@app.delete("/api/signals/{signal_id}")
def api_delete_signal(signal_id: str):
    """Delete a signal manually"""
    try:
        # FIX: Use actions_service directly instead of relying on global engine instance
        from api.actions_service import delete_signal

        result = delete_signal(signal_id)
        if result.get("success"):
             return {"status": "ok", "deleted": signal_id, "details": result}
        else:
             raise HTTPException(status_code=404, detail=result.get("error", "Signal not found"))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting signal: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/signals/manual")
def api_add_signal(signal_data: dict):
    """Add a signal manually for testing"""
    try:
        from core.signals import get_signal_manager
        from core.state_persistence import load_signals_payload, save_signals_payload

        # Load current signals
        signals_data = load_signals_payload({"open": {}, "closed": []})
        if not isinstance(signals_data, dict):
            signals_data = {"open": {}, "closed": []}

        # Create signal manager and add the signal
        signal_manager = get_signal_manager()
        signal_id = signal_manager.add_signal(signal_data)

        return {"status": "ok", "signal_id": signal_id}
    except Exception as e:
        logger.error(f"Error adding signal: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/performance/clear")
def api_clear_performance():
    """Clear all closed signals (performance history)"""
    try:
        signals_data = load_signals_payload({"open": {}, "closed": []})
        if not isinstance(signals_data, dict):
            signals_data = {"open": {}, "closed": []}
        
        # Clear closed signals array
        closed_count = len(signals_data.get("closed", []))
        signals_data["closed"] = []
        
        # Save updated signals
        save_signals_payload(signals_data)
        
        logger.info(f"Cleared {closed_count} closed signals from performance history")
        return {"status": "ok", "cleared": closed_count}
    
    except Exception as e:
        logger.error(f"Error clearing performance: {e}", exc_info=True)
        raise _safe_error()

@app.get("/api/state")
def api_get_state(
    include_news: bool = True,
    include_hotlist: bool = True,
    hotlist_details: bool = False,
    lean: bool = False,
    include: Optional[str] = None,
    live_prices: Optional[bool] = None,
    if_version: Optional[str] = None,
    unchanged: bool = True,
):
    """Get current application state including tickers, signals, stats, news"""
    try:
        include_flags = _parse_include_flags(include)
        capital_live_prices = False
        state_live_prices = False
        if include_flags == {"capital"}:
            requested_live_prices = bool(live_prices) if live_prices is not None else False
            capital_live_prices = bool(
                requested_live_prices
                and os.getenv("MS_CAPITAL_STATE_REMOTE_QUOTES", "0") == "1"
            )
        else:
            # Aggregate UI poll: serve engine/cached prices by default. The old
            # default refreshed the whole watchlist from Yahoo sequentially on
            # every cache miss, making /api/state take tens of seconds under
            # provider throttling (SLO breach, p95 in the seconds). Pass
            # live_prices=true to opt back into the synchronous refresh.
            state_live_prices = bool(live_prices) if live_prices is not None else False
        # --- endpoint-level response cache ---
        effective_live_prices = capital_live_prices if include_flags == {"capital"} else state_live_prices
        # A lean payload never contains news or the hot list, so those flags
        # do not change the response. The UI polls two lean variants
        # (include_hotlist true/false); keyed separately each one paid its own
        # cold build and its own background rebuilds for identical bytes.
        key_news, key_hotlist, key_details = (
            (False, False, False) if lean else (include_news, include_hotlist, hotlist_details)
        )
        cache_key = f"api_state:{key_news}:{key_hotlist}:{key_details}:{lean}:{include or ''}:{effective_live_prices}"
        # 4.5s: the Capital tab re-polls every 4-5s (own 4s client cache), so a
        # 3s server TTL guaranteed a full ~2.4MB rebuild on every poll
        # (audit 2026-09-05 P2: 3-12s responses). 4.5s keeps consecutive polls
        # on the cached payload while staying under one refresh interval.
        # Fresh windows under stale-while-revalidate: beyond them the last
        # snapshot answers instantly while one background thread rebuilds.
        # 45s/10s bound the rebuild churn (each build walks the full news and
        # capital corpora) while staying near the engine's 60s scan cadence.
        cache_ttl_seconds = 10.0 if include_flags == {"capital"} else 45.0
        bypass_cache = bool(include_flags == {"capital"} and capital_live_prices)
        cached = None if bypass_cache else _endpoint_payload_get(cache_key, ttl_seconds=cache_ttl_seconds)
        stale = None
        if cached is None and not bypass_cache:
            stale, _stale_age = _endpoint_payload_get_stale(cache_key)
            # A snapshot may bridge one rebuild, not stand in for the current
            # state for ever: the capital-only variant is requested rarely, and
            # a 10-minute-old account (balance, positions) was served as "now".
            max_stale_seconds = 30.0 if include_flags == {"capital"} else 120.0
            if stale is not None and (_stale_age is None or _stale_age > max_stale_seconds):
                stale = None
            if stale is not None:
                _kick_state_rebuild({
                    "cache_key": cache_key,
                    "include_flags": include_flags,
                    "include_news": include_news,
                    "include_hotlist": include_hotlist,
                    "hotlist_details": hotlist_details,
                    "lean": lean,
                    "state_live_prices": state_live_prices,
                    "capital_live_prices": capital_live_prices,
                })
        if cached is not None or stale is not None:
            # Even with cache, honour version-unchanged check
            if bool(unchanged) and if_version:
                try:
                    cached_dict = json.loads(cached if cached is not None else stale)
                    cached_ver = cached_dict.get("state_version", "")
                    if str(if_version).strip() == cached_ver:
                        return {"unchanged": True, "version": cached_ver}
                except Exception:
                    pass
            return Response(content=cached if cached is not None else stale, media_type="application/json")

        hotlist_epoch = _hotlist_membership_epoch()
        if include_flags == {"capital"}:
            state = get_capital_state(
                live_prices=capital_live_prices,
                include_opportunities=True,
                include_decisions=True,
            )
        else:
            state = get_state(
                include_news=include_news,
                include_hotlist=include_hotlist,
                hotlist_details=hotlist_details,
                lean=lean,
                live_prices=state_live_prices,
            )
        _VOLATILE_STATE_KEYS = {"timestamp", "api_stats", "capital", "log", "activity_log", "refresh_interval_ms"}
        version = _stable_payload_version(state if isinstance(state, dict) else {}, drop_keys=_VOLATILE_STATE_KEYS)
        if bool(unchanged) and if_version and str(if_version).strip() == version:
            return {"unchanged": True, "version": version}
        if isinstance(state, dict):
            state["state_version"] = version
            if include_flags:
                state = _filter_state_payload_include(state, include_flags)

        # Cache the full serialized response. HTTP callers receive these exact
        # bytes as well; returning the dict here made FastAPI traverse and JSON-
        # encode the complete state a second time on every cache miss, which
        # amplified cold UI polls into multi-second responses under agent load.
        payload_bytes = None
        if not bypass_cache:
            try:
                payload_bytes = json.dumps(state, ensure_ascii=False, default=str).encode("utf-8")
                # The hot list changed while this was being built: serve it,
                # but do not cache a pre-change snapshot.
                if hotlist_epoch == _hotlist_membership_epoch():
                    _endpoint_payload_set(cache_key, payload_bytes)
            except Exception:
                pass  # caching is best-effort
        if payload_bytes is not None and get_request_context() is not None:
            return Response(content=payload_bytes, media_type="application/json")
        return state
    except Exception as e:
        logger.error(f"Error getting state: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/dashboard")
def api_dashboard(include: Optional[str] = None, if_version: Optional[str] = None, unchanged: bool = True):
    """Home dashboard payload (V8)."""
    try:
        payload = get_dashboard_payload()
        version = _stable_payload_version(payload if isinstance(payload, dict) else {}, drop_keys={"dashboard_generated_at"})
        if bool(unchanged) and if_version and str(if_version).strip() == version:
            return {"unchanged": True, "version": version}
        if isinstance(payload, dict):
            payload["dashboard_state_version"] = version
            include_flags = _parse_include_flags(include)
            if include_flags:
                payload = _filter_dashboard_payload_include(payload, include_flags)
        return payload
    except Exception as e:
        logger.error(f"Error building dashboard payload: {e}", exc_info=True)
        raise _safe_error()


def _endpoint_payload_get_stale(cache_key: str):
    """Return (payload_bytes, age_seconds) ignoring TTL — for stale-while-revalidate serving."""
    with _ENDPOINT_PAYLOAD_CACHE_LOCK:
        entry = _ENDPOINT_PAYLOAD_CACHE.get(cache_key) or {}
        payload = entry.get("payload")
        ts = float(entry.get("ts") or 0.0)
        if payload is None:
            return None, None
        return payload, max(0.0, time.time() - ts)


_STATE_REBUILD_GUARD = threading.Lock()
_STATE_REBUILD_INFLIGHT: set = set()


def _build_and_cache_state_payload(params: Dict[str, Any]) -> None:
    """Rebuild one /api/state variant and refresh its cached bytes (background SWR)."""
    include_flags = params["include_flags"]
    hotlist_epoch = _hotlist_membership_epoch()
    if include_flags == {"capital"}:
        state = get_capital_state(
            live_prices=params["capital_live_prices"],
            include_opportunities=True,
            include_decisions=True,
        )
    else:
        state = get_state(
            include_news=params["include_news"],
            include_hotlist=params["include_hotlist"],
            hotlist_details=params["hotlist_details"],
            lean=params["lean"],
            live_prices=params["state_live_prices"],
        )
    _VOLATILE_STATE_KEYS = {"timestamp", "api_stats", "capital", "log", "activity_log", "refresh_interval_ms"}
    version = _stable_payload_version(state if isinstance(state, dict) else {}, drop_keys=_VOLATILE_STATE_KEYS)
    if isinstance(state, dict):
        state["state_version"] = version
        if include_flags:
            state = _filter_state_payload_include(state, include_flags)
    payload_bytes = json.dumps(state, ensure_ascii=False, default=str).encode("utf-8")
    if hotlist_epoch != _hotlist_membership_epoch():
        # Hot-list membership changed during the rebuild: this snapshot may
        # predate it, so it must not replace the cache that was just cleared.
        logger.info("State background rebuild for %s discarded (hot list changed).", params["cache_key"])
        return
    _endpoint_payload_set(params["cache_key"], payload_bytes)
    logger.info("State background rebuild completed for %s (%d bytes).", params["cache_key"], len(payload_bytes))


def _kick_state_rebuild(params: Dict[str, Any]) -> None:
    """Single-flight background rebuild of one /api/state cache variant.

    A cold /api/state build walks the full news/capital corpora (json decode,
    per-article to_dict, re-encode) and can take tens of seconds under agent
    load — callers must not block on it twice. Mirrors the /api/cockpit SWR.
    """
    key = str(params.get("cache_key") or "")
    with _STATE_REBUILD_GUARD:
        if key in _STATE_REBUILD_INFLIGHT:
            return
        _STATE_REBUILD_INFLIGHT.add(key)

    def _run() -> None:
        try:
            _build_and_cache_state_payload(params)
        except Exception:
            logger.warning("Background /api/state rebuild failed for %s", key, exc_info=True)
        finally:
            with _STATE_REBUILD_GUARD:
                _STATE_REBUILD_INFLIGHT.discard(key)

    threading.Thread(target=_run, name="state-swr-rebuild", daemon=True).start()


_COCKPIT_REBUILD_GUARD = threading.Lock()
_COCKPIT_REBUILD_STATE = {"running": False, "last_start": 0.0, "snapshot_started": 0.0}
_COCKPIT_BUILD_LOCK = threading.Lock()
_COCKPIT_VERSION_PREFIX = b'{"cockpit_state_version": "'


def _cockpit_encode_snapshot(payload: Any) -> bytes:
    """Serialize one cockpit snapshot with its content version as the FIRST key.

    The version is computed once per build. Polls then read it from the first
    bytes of the cached snapshot instead of re-parsing, re-hashing and
    re-encoding a ~1 MB document on every request.
    """
    body: Dict[str, Any] = {
        "cockpit_state_version": _cockpit_stable_version(payload if isinstance(payload, dict) else {}),
    }
    if isinstance(payload, dict):
        body.update({key: value for key, value in payload.items() if key != "cockpit_state_version"})
    # default=str keeps one non-JSON passthrough from 500-ing the whole
    # cockpit; allow_nan=False still refuses NaN/Infinity loudly per §99.
    return json.dumps(body, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")


def _cockpit_snapshot_version(snapshot: bytes) -> str:
    """Content version of a cached snapshot (prefix read; parse only as fallback)."""
    try:
        if snapshot.startswith(_COCKPIT_VERSION_PREFIX):
            start = len(_COCKPIT_VERSION_PREFIX)
            end = snapshot.find(b'"', start, start + 80)
            if end > start:
                return snapshot[start:end].decode("ascii")
        parsed = json.loads(snapshot)
        return _cockpit_stable_version(parsed if isinstance(parsed, dict) else {})
    except Exception:
        return "na"


def _cockpit_build_snapshot(not_before: float = 0.0) -> bytes:
    """Single-flight synchronous cockpit build.

    Only one build runs at a time. A caller that waited for the lock reuses
    the snapshot produced meanwhile when that build STARTED at or after
    ``not_before``: a cold request (0.0) takes whatever the warm-up just
    built instead of starting a duplicate build next to it, while an explicit
    ``fresh=true`` request only accepts a build that began after it arrived.
    """
    with _COCKPIT_BUILD_LOCK:
        if float(_COCKPIT_REBUILD_STATE.get("snapshot_started") or 0.0) >= float(not_before):
            existing, _existing_age = _endpoint_payload_get_stale("api_cockpit")
            if existing is not None:
                return existing
        started = time.time()
        snapshot = _cockpit_encode_snapshot(get_cockpit_payload())
        _endpoint_payload_set("api_cockpit", snapshot)
        _COCKPIT_REBUILD_STATE["snapshot_started"] = started
        return snapshot


def _kick_cockpit_rebuild() -> None:
    """Single-flight background rebuild of the cockpit read model.

    The payload aggregates 8 sub-sources and its cold build takes tens of
    seconds (news-state serialization dominates), so callers must never block
    on it twice: the endpoint serves the last snapshot and this thread
    refreshes the cache for the next poll.
    """
    with _COCKPIT_REBUILD_GUARD:
        if _COCKPIT_REBUILD_STATE["running"]:
            return
        if time.time() - float(_COCKPIT_REBUILD_STATE["last_start"] or 0.0) < 5.0:
            return
        _COCKPIT_REBUILD_STATE["running"] = True
        _COCKPIT_REBUILD_STATE["last_start"] = time.time()

    def _run() -> None:
        try:
            rebuild_started = time.time()
            payload_bytes = _cockpit_build_snapshot(not_before=rebuild_started)
            logger.info(
                "Cockpit background rebuild completed (%d bytes, %.1fs).",
                len(payload_bytes), time.time() - rebuild_started,
            )
        except Exception:
            logger.warning("Cockpit background rebuild failed", exc_info=True)
        finally:
            with _COCKPIT_REBUILD_GUARD:
                _COCKPIT_REBUILD_STATE["running"] = False

    threading.Thread(target=_run, name="cockpit-swr-rebuild", daemon=True).start()


@app.get("/api/cockpit")
def api_cockpit(
    if_version: Optional[str] = None,
    unchanged: bool = True,
    fresh: bool = False,
):
    """Bounded Phase 12 AI Trading Cockpit read model.

    Stale-while-revalidate: within the fresh window the cache answers
    directly; beyond it the last snapshot is served immediately while a
    single-flight background thread rebuilds the cache for the next poll.
    A true cold start (no snapshot at all) still builds synchronously so the
    full-payload contract is preserved — but single-flight: it joins a build
    that is already running (start-up warm-up) instead of duplicating it.

    The cached snapshot is returned as-is (bytes). Its content version sits in
    its first key, so an unchanged poll costs a prefix read, not a 1 MB
    decode + hash + re-encode.
    """
    try:
        cache_key = "api_cockpit"
        request_started = time.time()
        cached = None if fresh else _endpoint_payload_get(cache_key, ttl_seconds=60.0)
        stale = None
        if cached is None and not fresh:
            stale, _stale_age = _endpoint_payload_get_stale(cache_key)
            if stale is not None:
                _kick_cockpit_rebuild()
        snapshot = cached if cached is not None else stale
        if not isinstance(snapshot, (bytes, bytearray)):
            snapshot = _cockpit_build_snapshot(not_before=request_started if fresh else 0.0)
        version = _cockpit_snapshot_version(bytes(snapshot))
        if bool(unchanged) and if_version and str(if_version).strip() == version:
            return {"unchanged": True, "version": version}
        if not bytes(snapshot).startswith(_COCKPIT_VERSION_PREFIX):
            # Snapshot cached without the embedded version (older writer).
            legacy = json.loads(snapshot)
            if isinstance(legacy, dict):
                legacy["cockpit_state_version"] = version
            return legacy
        return Response(content=bytes(snapshot), media_type="application/json")
    except Exception as exc:
        logger.error("Error building AI Trading Cockpit payload: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/news")
def api_news():
    """Compatibility endpoint for News tab contracts."""
    try:
        state_resp = api_news_tab_state()
        if isinstance(state_resp, Response):
            body = getattr(state_resp, "body", b"{}") or b"{}"
            try:
                state = json.loads(body.decode("utf-8"))
            except Exception:
                state = {}
        else:
            state = state_resp or {}
        market_news = list((state or {}).get("market_news", []) or [])
        breaking = list((state or {}).get("breaking_news", []) or [])
        ticker_news = (state or {}).get("ticker_news", {}) or {}
        items = market_news + breaking
        for rows in ticker_news.values():
            if isinstance(rows, list):
                items.extend(rows)
        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "items": items,
            "market_news": market_news,
            "breaking_news": breaking,
            "disabled": bool((state or {}).get("disabled", False)),
            "total_items": len(items),
        }
    except Exception as e:
        logger.error(f"Error in /api/news: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/sec/{ticker}")
def api_sec_context(ticker: str, refresh: bool = False):
    """Return cache-first, point-in-time SEC filing metadata for a US equity."""
    try:
        return get_sec_context(ticker, refresh=refresh)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Error in /api/sec/%s: %s", ticker, exc, exc_info=True)
        raise _safe_error()


@app.get("/api/earnings")
def api_earnings(days: int = 14):
    """Compatibility endpoint for Earnings tab contracts."""
    try:
        from agents.economic_calendar_agent import get_economic_calendar_agent

        agent = get_economic_calendar_agent()
        cal_enabled = _agent_enabled("economic_calendar")
        data = agent.get_upcoming_events(days=days, allow_remote=False) or {}
        earnings = list((data or {}).get("earnings", []) or [])
        cache_manager.record_manual_decision(
            key=f"earnings:{int(days)}",
            decision="HIT",
            reason="calendar cache-only earnings read",
            source="earnings_endpoint",
            ttl_seconds=300,
            age_seconds=None,
        )
        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "days": int(days),
            "earnings": earnings,
            "disabled": not cal_enabled,
            "from_cache": True,
            "total": len(earnings),
        }
    except Exception as e:
        logger.error(f"Error in /api/earnings: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/signals")
def api_signals(limit: int = 100, offset: int = 0, history: bool = False):
    """Return a bounded signal window; opt into full-history pagination explicitly."""
    try:
        safe_limit = max(1, min(250, int(limit or 100)))
        safe_offset = max(0, int(offset or 0))
        payload = load_signals_payload(
            {"open": {}, "closed": []},
            include_closed=bool(history),
        )
        if not isinstance(payload, dict):
            payload = {"open": {}, "closed": []}
        payload = normalize_signal_payload_scores(payload)
        open_payload = payload.get("open", {}) if isinstance(payload.get("open"), dict) else {}
        closed_payload = payload.get("closed", []) if isinstance(payload.get("closed"), list) else []
        closed_payload = sorted(
            closed_payload,
            key=lambda row: str((row or {}).get("closed_at") or (row or {}).get("closedat") or (row or {}).get("created_at") or ""),
            reverse=True,
        )
        summary = payload.get("_summary") if isinstance(payload.get("_summary"), dict) else {}
        closed_count = int(summary.get("closed_count") or len(closed_payload))
        closed_page = closed_payload[safe_offset:safe_offset + safe_limit]
        source_is_complete = bool(history) or closed_count <= len(closed_payload)
        more_in_source = (safe_offset + len(closed_page)) < len(closed_payload)
        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "open": open_payload,
            "closed": closed_page,
            "open_count": len(open_payload),
            "closed_count": closed_count,
            "limit": safe_limit,
            "offset": safe_offset,
            "next_offset": safe_offset + len(closed_page) if more_in_source else None,
            "history_complete": source_is_complete,
        }
    except Exception as e:
        logger.error(f"Error in /api/signals: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/agents")
def api_agents():
    """Lightweight agents status endpoint for UI and diagnostics contract tests."""
    try:
        from api.state_service import get_shared_config

        config = get_shared_config()
        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "agents": snapshot_agent_runtimes(
                config=config,
                observed_status=snapshot_agent_status(),
                engine_running=is_engine_running(),
                agent_names=CONTROL_PLANE_AGENTS,
            ),
            "test_mode": IS_TEST_MODE,
        }
    except Exception as e:
        logger.error(f"Error in /api/agents: {e}", exc_info=True)
        raise _safe_error()


class AgentEnabledRequest(BaseModel):
    enabled: bool


class RiskSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_daily_loss_pct: float = Field(
        ge=MIN_DAILY_LOSS_PCT,
        le=MAX_DAILY_LOSS_PCT,
    )
    max_risk_per_trade_pct: float = Field(
        ge=MIN_RISK_PER_TRADE_PCT,
        le=MAX_RISK_PER_TRADE_PCT,
    )

    @field_validator("max_daily_loss_pct", "max_risk_per_trade_pct", mode="before")
    @classmethod
    def require_json_number(cls, value):
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("must be a JSON number")
        if not math.isfinite(float(value)):
            raise ValueError("must be finite")
        return float(value)


class RiskConstitutionRequest(BaseModel):
    """Complete editable shape; nullable fields are persisted only as a draft."""

    model_config = ConfigDict(extra="forbid")

    owner_confirmed: bool
    max_risk_per_trade_pct: Optional[float]
    max_position_size_pct: Optional[float]
    max_total_exposure_pct: Optional[float]
    max_leverage: Optional[int]
    minimum_cash_pct: Optional[float]
    max_sector_exposure_pct: Optional[float]
    correlation_threshold: Optional[float]
    max_correlated_exposure_pct: Optional[float]
    max_positions: Optional[int]
    max_liquidity_participation_pct: Optional[float]
    max_daily_loss_pct: Optional[float]
    max_daily_loss_trades: Optional[int]
    max_drawdown_pct: Optional[float]
    max_data_age_seconds: Optional[int]
    block_data_conflicts: Optional[bool]
    require_approved_strategy: Optional[bool]
    trade_policy: Optional[Dict[str, Any]] = None

    @field_validator("owner_confirmed", "block_data_conflicts", "require_approved_strategy", mode="before")
    @classmethod
    def require_json_boolean(cls, value):
        if value is None:
            return None
        if not isinstance(value, bool):
            raise ValueError("must be a JSON boolean")
        return value

    @field_validator(
        "max_risk_per_trade_pct",
        "max_position_size_pct",
        "max_total_exposure_pct",
        "minimum_cash_pct",
        "max_sector_exposure_pct",
        "correlation_threshold",
        "max_correlated_exposure_pct",
        "max_liquidity_participation_pct",
        "max_daily_loss_pct",
        "max_drawdown_pct",
        mode="before",
    )
    @classmethod
    def require_optional_json_number(cls, value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("must be a JSON number or null")
        if not math.isfinite(float(value)):
            raise ValueError("must be finite")
        return float(value)

    @field_validator("max_leverage", "max_positions", "max_daily_loss_trades", "max_data_age_seconds", mode="before")
    @classmethod
    def require_optional_json_integer(cls, value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("must be a JSON integer or null")
        number = float(value)
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError("must be an integer")
        return int(number)


class KillSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool

    @field_validator("active", mode="before")
    @classmethod
    def require_json_boolean(cls, value):
        if not isinstance(value, bool):
            raise ValueError("active must be a JSON boolean")
        return value


@app.get("/api/agents/control-plane")
def api_agents_control_plane():
    """Fast, bounded payload for the Agents automation control plane."""
    try:
        from api.state_service import get_shared_config
        from core.bot_controller import BotController

        config = get_shared_config()
        bot_status = BotController.get_instance(config_manager=config).get_control_status()
        runtime_agents = snapshot_agent_runtimes(
            config=config,
            observed_status=snapshot_agent_status(),
            engine_running=is_engine_running(),
            agent_names=CONTROL_PLANE_AGENTS,
        )
        # Same readiness the PaperBroker authorizer enforces; the tab derives
        # every "entries enabled / blocked" label from it (entry_gate).
        execution_control = None
        execution_control_error = None
        try:
            from api.execution_service import get_execution_control

            execution_control = get_execution_control(config)
        except Exception as control_exc:
            logger.warning("Control plane could not read paper-entry readiness: %s", control_exc)
            execution_control_error = "paper-entry readiness unavailable"
        return build_control_plane_payload(
            config=config,
            bot_status=bot_status,
            agents_status=runtime_agents,
            engine_running=is_engine_running(),
            execution_control=execution_control,
            execution_control_error=execution_control_error,
        )
    except Exception as exc:
        logger.error("Error in /api/agents/control-plane: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/agents/{agent_name}/enabled")
def api_set_agent_enabled(agent_name: str, request: AgentEnabledRequest):
    """Atomically enable or disable one supported agent."""
    try:
        from api.state_service import get_shared_config
        from core.bot_controller import BotController

        config = get_shared_config()
        bot_status = BotController.get_instance(config_manager=config).get_control_status()
        result = set_agent_enabled(
            config=config,
            agent_name=agent_name,
            enabled=request.enabled,
            bot_running=bool(bot_status.get("running")),
            status_updater=update_agent_status,
            runtime_switcher=lambda name, enabled: set_agent_runtime(
                name,
                enabled,
                config=config,
                engine_running=is_engine_running,
                start_engine=start_engine,
                test_mode=IS_TEST_MODE,
            ),
        )
        _raise_if_config_not_saved(config, "The agent on/off switch")
        return result
    except UnknownAgentError:
        raise HTTPException(status_code=404, detail="Unknown agent")
    except RequiredAgentRunningError:
        raise HTTPException(
            status_code=409,
            detail="Stop the bot before disabling a required safety agent",
        )
    except AgentRuntimeError as exc:
        logger.error("Agent %s runtime transition failed: %s", agent_name, exc)
        raise HTTPException(status_code=503, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to update agent %s: %s", agent_name, exc, exc_info=True)
        raise _safe_error()


@app.patch("/api/agents/risk/settings")
def api_set_risk_settings(request: RiskSettingsRequest):
    """Compatibility adapter; writes still flow through the canonical Risk Manager."""
    try:
        from api.state_service import get_shared_config

        result = set_risk_settings(
            config=get_shared_config(),
            max_daily_loss_pct=request.max_daily_loss_pct,
            max_risk_per_trade_pct=request.max_risk_per_trade_pct,
        )
        _raise_if_config_not_saved(get_shared_config(), "The Risk Manager limits")
        return result
    except (RiskLimitValidationError, RiskConstitutionSettingsError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to update Risk Manager settings: %s", exc, exc_info=True)
        raise _safe_error()


@app.patch("/api/agents/risk/constitution")
def api_set_risk_constitution(request: RiskConstitutionRequest):
    """Save/activate Constitution and trade policy in one atomic Risk Manager write."""
    try:
        from api.state_service import get_shared_config

        values = request.model_dump(exclude={"owner_confirmed", "trade_policy"})
        result = set_risk_constitution(
            config=get_shared_config(),
            values=values,
            owner_confirmed=request.owner_confirmed,
            trade_policy_values=request.trade_policy,
        )
        _endpoint_payload_clear("api_cockpit")
        _endpoint_payload_clear_prefix("api_state:")
        _raise_if_config_not_saved(get_shared_config(), "The Risk Manager settings")
        return result
    except RiskConstitutionSettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to update Risk Constitution: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/execution/control")
def api_execution_control():
    """Return owner controls and fail-closed Paper Auto readiness."""
    try:
        from api.execution_service import get_execution_control
        from api.state_service import get_shared_config

        return get_execution_control(get_shared_config())
    except Exception as exc:
        logger.error("Failed to read paper execution control: %s", exc, exc_info=True)
        raise _safe_error()


@app.patch("/api/execution/control")
def api_set_execution_control(_request: Optional[Dict[str, Any]] = None):
    """Reject the removed duplicate writer; Risk Manager is the only authority."""
    raise HTTPException(
        status_code=410,
        detail=(
            "Execution controls moved to Risk Manager. "
            "Use PATCH /api/agents/risk/constitution with trade_policy."
        ),
    )


@app.post("/api/execution/kill-switch")
def api_execution_kill_switch(request: KillSwitchRequest):
    """Immediately block/release new automated paper risk; monitoring stays active."""
    try:
        from api.execution_service import ExecutionControlError, set_kill_switch
        from api.state_service import get_shared_config

        result = set_kill_switch(config=get_shared_config(), active=request.active)
        _raise_if_config_not_saved(get_shared_config(), "The kill switch change")
        return result
    except ExecutionControlError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to update kill switch: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/execution/reconcile")
def api_reconcile_execution():
    """Reconcile the paper order ledger against authoritative Capital positions."""
    try:
        from api.execution_service import reconcile_execution
        from api.state_service import get_shared_config

        return reconcile_execution(get_shared_config())
    except Exception as exc:
        logger.error("Paper execution reconciliation failed: %s", exc, exc_info=True)
        raise _safe_error()


class OrphanedFillRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_intent_id: str
    reason: str


@app.post("/api/execution/reconcile/rollback-orphaned-fill")
def api_rollback_orphaned_fill(request: OrphanedFillRollbackRequest):
    """Owner repair: audit a FILLED open whose Capital position never persisted.

    Appends an audit event to the order ledger only; Capital cash and positions
    are never changed. Refused when the position exists or the fill moved cash.
    """
    try:
        from api.execution_service import ExecutionControlError, rollback_orphaned_fill
        from api.state_service import get_shared_config

        return rollback_orphaned_fill(
            get_shared_config(),
            order_intent_id=request.order_intent_id,
            reason=request.reason,
        )
    except ExecutionControlError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Orphaned fill rollback failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/execution/orders")
def api_execution_orders(limit: int = 100):
    """Return a bounded operational order summary without the heavy position payload."""
    try:
        from api.execution_service import list_execution_orders
        from api.state_service import get_shared_config

        return list_execution_orders(get_shared_config(), limit=limit)
    except Exception as exc:
        logger.error("Failed to read execution orders: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/ledger/decisions")
def api_ledger_decisions(limit: int = 50, ticker: str = "", source: str = ""):
    """Phase 13 §97 — persistent Decision Ledger rows (eleven-question view)."""
    try:
        from api.ledger_service import list_ledger_decisions
        from api.state_service import get_shared_config

        return list_ledger_decisions(
            get_shared_config(), limit=limit,
            ticker=(ticker or "").strip() or None,
            source=(source or "").strip() or None,
        )
    except Exception as exc:
        logger.error("Failed to read decision ledger: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/ledger/decisions/{decision_id}")
def api_ledger_decision(decision_id: str):
    """Phase 13 §97 — one ledger entry with its full outcome history."""
    try:
        from api.ledger_service import LedgerServiceError, get_ledger_decision
        from api.state_service import get_shared_config

        return get_ledger_decision(get_shared_config(), decision_id)
    except LedgerServiceError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="decision not found")
    except Exception as exc:
        logger.error("Failed to read ledger decision %s: %s", decision_id, exc, exc_info=True)
        raise _safe_error()


@app.get("/api/ledger/no-trade-evaluation")
def api_ledger_no_trade_evaluation(
    threshold_pct: float = 1.0, horizon_sessions: int = 0, limit: int = 200,
    backfill: Optional[bool] = None,
):
    """Phase 13 §90 — rejection quality of recorded no-trade decisions."""
    try:
        from api.ledger_service import get_no_trade_evaluation
        from api.state_service import get_shared_config

        return get_no_trade_evaluation(
            get_shared_config(),
            threshold_pct=threshold_pct,
            horizon_sessions=int(horizon_sessions) or None,
            limit=limit,
            backfill=backfill,
        )
    except Exception as exc:
        logger.error("No-trade evaluation failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/ledger/model-lab")
def api_ledger_model_lab(role: str = "", limit: int = 20):
    """Phase 13 §88 — per-role model comparisons (no global score)."""
    try:
        from api.ledger_service import LedgerServiceError, get_model_lab
        from api.state_service import get_shared_config

        return get_model_lab(get_shared_config(), role=(role or "").strip() or None, limit=limit)
    except LedgerServiceError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.error("Model Lab read failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/ledger/replay")
def api_ledger_replay(payload: dict):
    """Phase 13 §89 — replay one decision at its original point in time.

    The replay is an audit artifact recorded in the ledger; it can never
    create an order or change positions.
    """
    try:
        from api.ledger_service import LedgerServiceError, replay_decision
        from api.state_service import get_shared_config

        if not isinstance(payload, dict):
            raise LedgerServiceError("JSON body required")
        decision_id = str(payload.get("decision_id") or "").strip()
        if not decision_id:
            raise LedgerServiceError("decision_id is required")
        model = str(payload.get("model") or "").strip() or None
        return replay_decision(get_shared_config(), decision_id, model=model)
    except LedgerServiceError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="decision not found")
    except Exception as exc:
        logger.error("Decision replay failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/research/weaknesses")
def api_research_weaknesses():
    """Phase 14 §111 — deterministic weakness observations over closed trades."""
    try:
        from api.research_factory_service import get_weaknesses

        return get_weaknesses()
    except Exception as exc:
        logger.error("Research weaknesses failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/research/hypotheses")
def api_research_hypotheses(limit: int = 50):
    """Phase 14 §76 — structured hypothesis records."""
    try:
        from api.research_factory_service import get_hypotheses

        return get_hypotheses(limit=limit)
    except Exception as exc:
        logger.error("Research hypotheses failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/research/experiments")
def api_research_experiments(limit: int = 50, status: str = ""):
    """Phase 14 §77 — experiment registry with the nine lifecycle statuses."""
    try:
        from api.research_factory_service import get_experiments

        return get_experiments(limit=limit, status=(status or "").strip().upper() or None)
    except Exception as exc:
        logger.error("Research experiments failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/research/drift")
def api_research_drift():
    """Phase 14 §79 — drift verdicts; REVALIDATION only, never risk changes."""
    try:
        from api.research_factory_service import get_drift

        return get_drift()
    except Exception as exc:
        logger.error("Research drift failed: %s", exc, exc_info=True)
        raise _safe_error()




@app.post("/api/research/run-cycle")
def api_research_run_cycle(payload: dict):
    """Phase 14 §111 — one bounded research cycle (research-only, no orders)."""
    try:
        from api.research_factory_service import run_cycle

        if payload is None:
            payload = {}
        max_experiments = payload.get("max_experiments", 3) if isinstance(payload, dict) else 3
        return run_cycle(max_experiments=int(max_experiments))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="max_experiments must be an integer")
    except Exception as exc:
        logger.error("Research cycle failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/pre-earnings/continuous-scan")
def api_pe_continuous_scan_status():
    """Continuous Pre-Earnings scanner status (1 ticker/interval + AI)."""
    try:
        from api.research_factory_service import pe_scan_status

        return pe_scan_status()
    except Exception as exc:
        logger.error("Continuous pre-earnings status failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/pre-earnings/continuous-scan/start")
def api_pe_continuous_scan_start():
    try:
        from api.research_factory_service import pe_scan_start

        return pe_scan_start()
    except Exception as exc:
        logger.error("Continuous pre-earnings start failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/pre-earnings/continuous-scan/stop")
def api_pe_continuous_scan_stop():
    try:
        from api.research_factory_service import pe_scan_stop

        return pe_scan_stop()
    except Exception as exc:
        logger.error("Continuous pre-earnings stop failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/pre-earnings/continuous-scan/once")
def api_pe_continuous_scan_once():
    """Scan the next round-robin ticker immediately (data + AI verdict)."""
    try:
        from api.research_factory_service import pe_scan_once

        return pe_scan_once()
    except Exception as exc:
        logger.error("Continuous pre-earnings once failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/market/listing")
def api_market_listing(ticker: str = ""):
    """Listing exchange of a US stock or ETF, so that the chart opens on the right exchange."""
    try:
        from core.strategy_lab.data import stock_listing

        return stock_listing(ticker[:20])
    except Exception as exc:
        logger.error("Listing lookup failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/learning/state")
def api_learning_state():
    """Learning mode: progress toward 10,000 paper trades Opus chose, today's count, a pause and its reason."""
    try:
        from api.learning_service import learning_state

        return learning_state()
    except Exception as exc:
        logger.error("Learning-mode state failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/strategy-lab/state")
def api_strategy_lab_state():
    """Strategy Lab: what was tested, what passed, the research lead's verdicts, what trades on paper."""
    try:
        from api.strategy_lab_service import strategy_lab_state

        return strategy_lab_state()
    except Exception as exc:
        logger.error("Strategy Lab state failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/strategy-lab/run")
def api_strategy_lab_run():
    """Start one Strategy Lab run now (in the background, in its own process)."""
    try:
        from api.strategy_lab_service import strategy_lab_run_now

        return strategy_lab_run_now()
    except Exception as exc:
        logger.error("Strategy Lab run request failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/research/auto-cycle")
def api_research_auto_cycle_status():
    """Phase 14 §111 — automatic research-cycle scheduler status."""
    try:
        from api.research_factory_service import auto_cycle_status

        return auto_cycle_status()
    except Exception as exc:
        logger.error("Research auto-cycle status failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/research/auto-cycle/start")
def api_research_auto_cycle_start():
    """Phase 14 §111 — owner enables the automatic research cycle."""
    try:
        from api.research_factory_service import auto_cycle_start

        return auto_cycle_start()
    except Exception as exc:
        logger.error("Research auto-cycle start failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/research/auto-cycle/stop")
def api_research_auto_cycle_stop():
    """Phase 14 §111 — owner disables the automatic research cycle."""
    try:
        from api.research_factory_service import auto_cycle_stop

        return auto_cycle_stop()
    except Exception as exc:
        logger.error("Research auto-cycle stop failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/research/auto-cycle/run")
def api_research_auto_cycle_run():
    """Phase 14 §111 — trigger one bounded cycle now via the scheduler."""
    try:
        from api.research_factory_service import auto_cycle_run_now

        return auto_cycle_run_now()
    except Exception as exc:
        logger.error("Research auto-cycle run-now failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/development/submissions")
def api_development_submissions(limit: int = 50):
    """Phase 15 §112 — autonomous-development submission ledger (sandbox only)."""
    try:
        from core.development_factory.review import ReviewLedger

        ledger = ReviewLedger()
        from core.development_factory.pipeline import submission_summary

        submissions = [submission_summary(s) for s in ledger.list_submissions(limit=limit)]
        return {"submissions": submissions, "count": len(submissions)}
    except Exception as exc:
        logger.error("Development submissions read failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/development/submissions")
def api_development_submit(payload: dict):
    """Phase 15 §112 — policy-guard one autonomous diff and open reviews.

    Guard + ledger only (apply=False): remote callers cannot mutate the
    repository; worktree application runs through the internal pipeline.
    """
    try:
        from core.config import BASE_DIR
        from core.development_factory.pipeline import DevelopmentPipelineError, submit_change
        from core.development_factory.review import ReviewLedger

        if not isinstance(payload, dict):
            raise DevelopmentPipelineError("JSON body required")
        experiment_id = str(payload.get("experiment_id") or "").strip()
        diff_text = str(payload.get("diff") or "")
        author = str(payload.get("author") or "coding_agent").strip()
        if not experiment_id or not diff_text:
            raise DevelopmentPipelineError("experiment_id and diff are required")
        result = submit_change(
            repo_path=BASE_DIR, experiment_id=experiment_id, author=author,
            diff_text=diff_text, ledger=ReviewLedger(), apply=False,
        )
        return result
    except DevelopmentPipelineError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.error("Development submission failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/development/submissions/{submission_id}/review")
def api_development_review(submission_id: str, payload: dict):
    """Phase 15 §81 — record one review stage (actor must differ from author)."""
    try:
        from core.development_factory.pipeline import submission_summary
        from core.development_factory.review import ReviewError, ReviewLedger

        if not isinstance(payload, dict):
            raise ReviewError("JSON body required")
        ledger = ReviewLedger()
        submission = ledger.record_review(
            submission_id,
            stage=str(payload.get("stage") or ""),
            actor=str(payload.get("actor") or ""),
            verdict=str(payload.get("verdict") or "").upper(),
            evidence=payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {},
        )
        return {"submission": submission_summary(submission)}
    except ReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.error("Development review failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/development/auto-repair")
def api_development_auto_repair(payload: dict):
    """§112 — run the bounded coding-agent self-repair loop (owner-gated, sandbox only).

    The Coding Agent LLM generates a diff, policy/sandbox/review gates validate
    it, and every failure is fed back for a bounded regeneration. Nothing here
    merges, deploys or promotes (Phase 15 boundary).
    """
    try:
        from core.config import BASE_DIR
        from core.development_factory.auto_repair import (
            AutoRepairError, RepairTask, auto_repair_config, build_task_from_experiment,
            run_auto_repair,
        )
        from core.development_factory.review import ReviewLedger
        from api.state_service import get_shared_config

        config = get_shared_config()
        cfg = auto_repair_config(config)
        if not cfg["enabled"]:
            raise HTTPException(
                status_code=403,
                detail="auto-repair is disabled — enable development_factory.auto_repair.enabled in config",
            )
        if not isinstance(payload, dict):
            raise AutoRepairError("JSON body required")
        experiment_id = str(payload.get("experiment_id") or "").strip()
        problem = str(payload.get("problem") or "").strip()
        if experiment_id:
            task = build_task_from_experiment(experiment_id)
        elif problem:
            task = RepairTask(
                experiment_id=str(payload.get("run_id") or "repair-adhoc"),
                problem=problem,
                target_hint=str(payload.get("target_hint") or ""),
            )
        else:
            raise AutoRepairError("experiment_id or problem is required")
        max_attempts = int(payload.get("max_attempts") or cfg["max_attempts"])
        return run_auto_repair(
            task, config=config,
            max_attempts=max_attempts, repo_path=BASE_DIR, ledger=ReviewLedger(),
        )
    except HTTPException:
        raise
    except AutoRepairError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.error("Development auto-repair failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/development/repair-cases")
def api_development_repair_cases(limit: int = 20):
    """§112 — the autonomous repair card: every case, every agent, every verdict."""
    try:
        from core.development_factory.watchdog import RepairCaseStore, case_summary_rows

        rows = case_summary_rows(RepairCaseStore(), limit=limit)
        return {"cases": rows, "count": len(rows)}
    except Exception as exc:
        logger.error("Repair cases read failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/development/repair-watchdog")
def api_development_repair_watchdog():
    """§112 — watchdog status (detect→coding→review→auto-apply→post-check)."""
    try:
        from core.development_factory.watchdog import get_repair_watchdog, watchdog_config
        from api.state_service import get_shared_config

        cfg = watchdog_config(get_shared_config())
        status = get_repair_watchdog(get_shared_config()).status()
        return {"config": cfg, **status,
                "note": "independent reviewers' PASS auto-applies the fix; "
                        "post-apply sanity failure rolls back automatically"}
    except Exception as exc:
        logger.error("Repair watchdog status failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/development/repair-watchdog/start")
def api_development_repair_watchdog_start():
    try:
        from api.state_service import get_shared_config
        from core.development_factory.watchdog import get_repair_watchdog

        config = get_shared_config()
        started = get_repair_watchdog(config).start()
        _persist_watchdog_enabled(config, True)
        return {"started": started, **get_repair_watchdog(config).status()}
    except Exception as exc:
        logger.error("Repair watchdog start failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/development/repair-watchdog/stop")
def api_development_repair_watchdog_stop():
    try:
        from api.state_service import get_shared_config
        from core.development_factory.watchdog import get_repair_watchdog

        config = get_shared_config()
        was_running = get_repair_watchdog(config).stop()
        _persist_watchdog_enabled(config, False)
        return {"stopped": was_running, **get_repair_watchdog(config).status()}
    except Exception as exc:
        logger.error("Repair watchdog stop failed: %s", exc, exc_info=True)
        raise _safe_error()


def _persist_watchdog_enabled(config: Any, enabled: bool) -> None:
    update_value = getattr(config, "update_value", None)
    if callable(update_value):
        update_value(["development_factory", "auto_repair", "watchdog_enabled"], enabled)


@app.post("/api/development/repair-watchdog/run")
def api_development_repair_watchdog_run():
    """Owner-triggered immediate pass: detect problems, run the repair chain."""
    try:
        from api.state_service import get_shared_config
        from core.development_factory.watchdog import run_watchdog_cycle

        result = run_watchdog_cycle(config=get_shared_config())
        return {"result": result}
    except Exception as exc:
        logger.error("Repair watchdog run failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/development/repair-cases/{case_id}/dismiss")
def api_development_repair_case_dismiss(case_id: str):
    try:
        from core.development_factory.watchdog import RepairCaseStore, WatchdogError

        store = RepairCaseStore()
        case = store.get(case_id)
        if case is None:
            raise WatchdogError(f"unknown case_id: {case_id}")
        updated = store.update(case_id, status="DISMISSED",
                               _note="dismissed by owner")
        return {"case": {k: updated.get(k) for k in
                         ("case_id", "status", "title", "closed_at")}}
    except WatchdogError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.error("Repair case dismiss failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/development/repair-cases/{case_id}/approve")
def api_development_repair_case_approve(case_id: str):
    """Owner approves a PROPOSED improvement: apply + post-check + rollback."""
    try:
        from core.development_factory.watchdog import (
            WatchdogError, approve_case,
        )

        updated = approve_case(case_id)
        return {"case": {k: updated.get(k) for k in
                         ("case_id", "status", "title", "applied_files",
                          "post_check", "applied_at", "error")}}
    except WatchdogError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.error("Repair case approve failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/development/repair-cases/{case_id}/run")
def api_development_repair_case_run(case_id: str):
    """Run a specific case on-demand through the Coding Agent and review chain."""
    try:
        from core.development_factory.watchdog import (
            RepairCaseStore, WatchdogError, run_case,
        )
        from api.state_service import get_shared_config

        store = RepairCaseStore()
        case = store.get(case_id)
        if case is None:
            raise WatchdogError(f"unknown case_id: {case_id}")

        if case.get("status") != "DETECTED":
            # Owner on-demand reopen: reset the attempt budget so the Coding
            # Agent gets a fresh chain instead of failing on exhausted attempts.
            store.update(case_id, status="DETECTED", attempts=[],
                         _note="reopened by owner for on-demand run")

        result = run_case(case_id, config=get_shared_config(), store=store)
        return {"case": result}
    except WatchdogError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.error("Repair case run failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/ui-inspector/status")
def api_ui_inspector_status():
    """Returns the latest tab-by-tab inspection report and defect escalations."""
    try:
        from agents.ui_inspector_agent import get_ui_inspector_agent, get_ui_inspector_agent_if_ready
        from api.state_service import get_shared_config

        agent = get_ui_inspector_agent_if_ready() or get_ui_inspector_agent(get_shared_config())
        return agent.get_inspection_report()
    except Exception as exc:
        logger.error("UI Inspector status read failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/ui-inspector/run")
def api_ui_inspector_run():
    """Triggers an immediate tab-by-tab inspection cycle."""
    try:
        from agents.ui_inspector_agent import get_ui_inspector_agent, get_ui_inspector_agent_if_ready
        from api.state_service import get_shared_config

        agent = get_ui_inspector_agent_if_ready() or get_ui_inspector_agent(get_shared_config())
        return agent.run_inspection_cycle()
    except Exception as exc:
        logger.error("UI Inspector run cycle failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/ui-inspector/layout-report")
def api_ui_inspector_layout_report(payload: dict):
    """Browser-side layout audit findings (text overflow etc.) -> Coding Agent cases."""
    try:
        from agents.ui_inspector_agent import get_ui_inspector_agent, get_ui_inspector_agent_if_ready
        from api.state_service import get_shared_config

        agent = get_ui_inspector_agent_if_ready() or get_ui_inspector_agent(get_shared_config())
        return agent.record_layout_report(payload)
    except Exception as exc:
        logger.error("UI Inspector layout report failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/development/repair-agent")
def api_development_repair_agent(limit: int = 20):
    """The Coding Agent tab: live state, full journeys, proposals, history."""
    try:
        from core.development_factory.watchdog import (
            RepairCaseStore, get_repair_watchdog, repair_agent_view,
            watchdog_config,
        )
        from api.state_service import get_shared_config

        config = get_shared_config()
        view = repair_agent_view(RepairCaseStore(), limit=max(1, min(50, limit)))
        status = get_repair_watchdog(config).status()
        cfg = watchdog_config(config)
        return {**view, "watchdog": {
            "running": status.get("running"),
            "thread_alive": status.get("thread_alive"),
            "stalled": status.get("stalled"),
            "cycle_started_at": status.get("cycle_started_at"),
            "cycle_age_seconds": status.get("cycle_age_seconds"),
            "last_heartbeat": status.get("last_heartbeat"),
            "stale_after_seconds": status.get("stale_after_seconds"),
            "interval_seconds": status.get("interval_seconds"),
            "cycles_completed": status.get("cycles_completed"),
            "last_run": status.get("last_run"),
            "last_error": status.get("last_error"),
            "improvements_enabled": cfg.get("improvements_enabled"),
            "max_attempts": cfg.get("max_attempts"),
        }}
    except Exception as exc:
        logger.error("Repair agent view failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/ml/status")
def api_ml_status():
    """Get ML model registry status, artifact verification, and training readiness."""
    try:
        from core.ml.trainer import get_ml_status
        from api.state_service import get_shared_config

        return get_ml_status(get_shared_config())
    except Exception as exc:
        logger.error("ML status read failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/ml/registry")
def api_ml_registry():
    """Get raw ML registry records."""
    try:
        from core.ml.trainer import _registry_from_config
        from api.state_service import get_shared_config

        return {"registry": _registry_from_config(get_shared_config())}
    except Exception as exc:
        logger.error("ML registry read failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/ml/train")
def api_ml_train(payload: Optional[Dict[str, Any]] = None):
    """One-click runner to train baseline model, isotonic calibration, and meta-labeling."""
    try:
        from core.ml.trainer import train_and_register_baseline
        from api.state_service import get_shared_config

        data = payload if isinstance(payload, dict) else {}
        force = bool(data.get("force", True))
        result = train_and_register_baseline(get_shared_config(), force=force)
        return result
    except Exception as exc:
        logger.error("ML training failed: %s", exc, exc_info=True)
        return {"status": "ERROR", "detail": str(exc)}


@app.get("/api/promotion/records")
def api_promotion_records(limit: int = 50):
    """Phase 16 §87 — promotion records with rollback conditions."""
    try:
        from core.promotion.records import PromotionRecordStore

        records = PromotionRecordStore().list_records(limit=limit)
        return {"promotions": [record.to_dict() for record in records], "count": len(records)}
    except Exception as exc:
        logger.error("Promotion records read failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/promotion/records/{promotion_id}/evaluate-rollback")
def api_promotion_evaluate_rollback(promotion_id: str, payload: dict):
    """Phase 16 §87/GOLD-017 — evaluate recorded rollback conditions.

    Restores the last known valid champion when a configured regression
    threshold is breached; never changes risk limits.
    """
    try:
        from core.promotion.lifecycle import evaluate_rollback
        from core.promotion.records import PromotionRecordError, PromotionRecordStore
        from core.research_factory.experiments import ExperimentRegistry
        from core.research_factory.hypothesis import HypothesisStore

        metrics = payload.get("metrics") if isinstance(payload, dict) and isinstance(payload.get("metrics"), dict) else {}
        return evaluate_rollback(
            promotion_id, metrics=metrics,
            promotion_store=PromotionRecordStore(),
            experiment_registry=ExperimentRegistry(hypothesis_store=HypothesisStore()),
        )
    except PromotionRecordError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="promotion not found")
    except Exception as exc:
        logger.error("Rollback evaluation failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.post("/api/agents/risk/run-cycle")
@app.post("/api/agents/autonomous/run-once", include_in_schema=False)
def api_run_risk_manager_cycle_once():
    """Run one gated Risk Manager evaluation cycle for the active paper bot."""
    try:
        controller = _get_bot_controller()
        status = controller.get_status()
        if not bool(status.get("running")):
            raise HTTPException(status_code=409, detail="Start the paper bot before running a Risk Manager cycle")
        if str(status.get("mode") or "paper").lower() != "paper":
            raise HTTPException(status_code=409, detail="Manual Risk Manager cycles are restricted to paper mode")

        from api.execution_service import get_execution_control
        from api.state_service import get_shared_config

        execution_control = get_execution_control(get_shared_config())
        readiness = execution_control.get("readiness") or {}
        if not bool(readiness.get("ready")):
            reasons = readiness.get("reasons") or ["Paper Auto is not ready"]
            raise HTTPException(status_code=409, detail="; ".join(str(item) for item in reasons))

        # Only the internal candidate worker reads the queued request. With it
        # switched off the request sat in the queue for ever while the UI
        # reported "cycle triggered".
        shared_config = get_shared_config()
        agents_cfg = getattr(shared_config, "agents", None)
        execution_cfg = agents_cfg.get("execution") if isinstance(agents_cfg, dict) else None
        execution_cfg = execution_cfg if isinstance(execution_cfg, dict) else {}
        if execution_cfg.get("autonomous_candidates_enabled") is not True:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The internal candidate worker is off "
                    "(agents.execution.autonomous_candidates_enabled), so no cycle can run."
                ),
            )

        from core.autonomous_control import request_autonomous_cycle

        return {"mode": "paper", "request": request_autonomous_cycle()}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Manual Risk Manager cycle failed: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/agents/risk/pipeline-status")
@app.get("/api/agents/autonomous/status", include_in_schema=False)
def api_risk_manager_cycle_status():
    """Return the internal candidate worker status owned by Risk Manager."""
    try:
        from core.autonomous_control import get_autonomous_cycle_status

        return get_autonomous_cycle_status()
    except Exception as exc:
        logger.error("Unable to read autonomous cycle status: %s", exc, exc_info=True)
        raise _safe_error()


@app.get("/api/config")
def api_get_config():
    """Get current configuration"""
    try:
        config = get_config()
        return config
    except Exception as e:
        logger.error(f"Error getting config: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/config")
def api_save_config(request: ConfigUpdateRequest):
    """Save/update configuration"""
    try:
        assert_no_risk_manager_owned_updates(request.config)
        prev_cfg = get_config()
        save_config(request.config)
        new_cfg = get_config()
        _apply_runtime_agent_switches(prev_cfg, new_cfg)
        # _apply_runtime_agent_switches only covers the four loop-driven agents
        # (trade_radar / strategy_research / premarket_scanner / pre_earnings).
        # Scanner, sentiment, news and the rest are managed by AgentManager, which
        # reads the freshly-saved config in start_agents(). Without this call,
        # toggling those agents in Global Settings only took effect after a full
        # server restart — a silent "config change ignored" gap (directive: every
        # control live, nothing hardcoded/stale).
        try:
            from api.engine_service import get_engine
            engine = get_engine()
            if engine and hasattr(engine, "agent_manager") and engine.agent_manager:
                engine.agent_manager.update_config()
        except Exception as _e:
            logger.warning(f"agent_manager.update_config skipped after config save: {_e}")
        return {"status": "ok"}
    except RiskConstitutionSettingsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ConfigValidationError as e:
        # Non-finite / out-of-range setting: nothing was written.
        raise HTTPException(status_code=422, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving config: {e}", exc_info=True)
        raise _safe_error()

# --- §108 AI configuration journey (spec §34–§38, §106 keys never to frontend) ---

@app.get("/api/ai/providers")
def api_ai_providers():
    """§108 — list configured AI providers (has_key only, never the key — §106)."""
    try:
        return list_ai_providers(get_config())
    except Exception as e:
        logger.error(f"Error listing AI providers: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/ai/providers/{name}/test")
def api_ai_test_provider(name: str):
    """§108 — Test Connection."""
    try:
        return probe_ai_provider(get_config(), name)
    except Exception as e:
        logger.error(f"Error testing AI provider {name}: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/ai/providers/{name}/discover")
def api_ai_discover_models(name: str):
    """§108 — discover models into the registry."""
    try:
        return discover_ai_models(get_config(), name)
    except Exception as e:
        logger.error(f"Error discovering models for {name}: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/ai/providers/{name}/key")
def api_ai_set_provider_key(name: str, payload: dict):
    """§108/§106 — owner sets a provider API key (write-only, stored in .env;
    the key value is never returned to the frontend)."""
    try:
        from api.ai_config_service import set_provider_api_key

        if not isinstance(payload, dict) or "api_key" not in payload:
            raise HTTPException(status_code=422, detail="JSON body with api_key is required")
        return set_provider_api_key(get_config(), name, str(payload.get("api_key") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting API key for {name}: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/ai/providers/{name}/select")
def api_ai_select_provider(name: str, payload: dict):
    """§108 — make this provider (and model) the global AI for the decision engine."""
    try:
        from api.ai_config_service import set_global_ai_provider

        model = str((payload or {}).get("model") or "").strip() if isinstance(payload, dict) else ""
        return set_global_ai_provider(get_config(), name, model)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as e:
        logger.error(f"Error selecting AI provider {name}: {e}", exc_info=True)
        raise _safe_error()

@app.get("/api/ai/assignment")
def api_ai_get_assignment():
    try:
        return get_ai_assignment(get_config())
    except Exception as e:
        logger.error(f"Error getting AI assignment: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/ai/assignment")
def api_ai_set_assignment(request: AIAssignmentRequest):
    """§108 — select role models → save."""
    try:
        set_ai_assignment(get_config(), request.assignment)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error saving AI assignment: {e}", exc_info=True)
        raise _safe_error()

@app.get("/api/ai/router")
def api_ai_get_router():
    try:
        return get_ai_router(get_config())
    except Exception as e:
        logger.error(f"Error getting AI router: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/ai/router")
def api_ai_set_router(request: AIRouterRequest):
    try:
        set_ai_router(get_config(), request.router)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error saving AI router: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/ai/request")
def api_ai_validated_request(request: AIValidatedRequest):
    """§108 — make a validated request (no key in response — §106)."""
    try:
        return ai_validated_request(get_config(), request.provider, request.prompt, model=request.model, system=request.system)
    except Exception as e:
        logger.error(f"Error in AI validated request: {e}", exc_info=True)
        raise _safe_error()

def _verify_new_ticker(ticker: str) -> Dict[str, Any]:
    """Existence check for Add ticker. Never raises; bounded by a short timeout.

    Env: MS_ADD_TICKER_VERIFY=0 turns the check off (it is off in test mode
    unless set to 1); MS_ADD_TICKER_VERIFY_TIMEOUT_SECONDS (default 5).
    """
    flag = str(os.getenv("MS_ADD_TICKER_VERIFY", "") or "").strip().lower()
    if flag in {"0", "false", "no", "off"} or (IS_TEST_MODE and flag not in {"1", "true", "yes", "on"}):
        return {"status": "skipped", "ticker": ticker, "source": "none",
                "reason": "symbol verification is disabled"}
    try:
        timeout_seconds = float(os.getenv("MS_ADD_TICKER_VERIFY_TIMEOUT_SECONDS", "5") or 5)
    except (TypeError, ValueError):
        timeout_seconds = 5.0
    result: Any = None
    try:
        from api.market_data_service import verify_symbol_exists

        result = verify_symbol_exists(ticker, timeout_seconds=timeout_seconds)
    except Exception as exc:
        logger.warning("Symbol verification failed for %s: %s", ticker, exc)
    if not isinstance(result, dict) or result.get("status") not in {"found", "not_found", "inactive", "unverified"}:
        return {"status": "unverified", "ticker": ticker, "source": "none",
                "reason": "the quote lookup could not be completed"}
    return result


@app.post("/api/tickers")
def api_add_ticker(request: TickerRequest):
    """Add ticker to watchlist"""
    try:
        ticker = request.ticker.strip().upper()
        if not ticker:
            raise HTTPException(status_code=400, detail="Ticker cannot be empty")
        # A symbol is letters/digits plus . - = ^ _ / : (BRK.B, BTC-USD, GC=F,
        # ^GSPC, VANTI.PA). Anything else (spaces, HTML, quotes) was stored
        # as-is and then rendered into every tab that lists the watchlist.
        if not re.fullmatch(r"[A-Z0-9^][A-Z0-9.\-=^_/:]{0,23}", ticker):
            raise HTTPException(
                status_code=400,
                detail="Invalid ticker symbol. Use letters, digits and . - = ^ only (e.g. AAPL, BTC-USD, GC=F).",
            )
        # The format check alone let any well-formed typo into the watchlist
        # (it then sat there as a permanent "no data" row). A symbol the data
        # provider definitively does not know is refused unless force=true; a
        # lookup that could not be completed never blocks the add.
        force = bool(getattr(request, "force", False))
        verification = (
            {"status": "forced", "ticker": ticker, "source": "none", "reason": "added without a quote check (force)"}
            if force else _verify_new_ticker(ticker)
        )
        if verification.get("status") in {"not_found", "inactive"}:
            last_trade = str(verification.get("last_trade") or "")[:10]
            detail = (
                f"No live quote for {ticker} — last trade {last_trade}, it looks delisted or suspended"
                if verification.get("status") == "inactive" and last_trade
                else f"No quote found for {ticker} — check the symbol"
            )
            return JSONResponse(
                status_code=422,
                content={
                    "status": "error",
                    "ok": False,
                    "detail": detail,
                    "error_code": "symbol_not_found",
                    "code": "no_quote",
                    "ticker": ticker,
                    "added": False,
                    "verified": False,
                    "quote_found": False,
                    "verify_status": verification.get("status"),
                    "can_force": True,
                    "requires_force": True,
                },
            )
        result = add_ticker(ticker, return_state=bool(request.return_state))
        if isinstance(result, dict):
            result["verified"] = verification.get("status") == "found"
            result["verify_status"] = verification.get("status")
            if not result["verified"]:
                result["verify_note"] = str(verification.get("reason") or "quote check not completed")

        # Trigger proactive calendar refresh for the new ticker (background)
        def refresh_calendar_background(t: str):
            if not _agent_enabled("economic_calendar"):
                return
            try:
                from agents.economic_calendar_agent import get_economic_calendar_agent
                agent = get_economic_calendar_agent()
                agent.invalidate_earnings_cache(t) # Clear specific cache
                agent.refresh_data() # Re-populate
            except Exception as e:
                logger.error(f"Failed to refresh calendar after adding {t}: {e}")
        
        # Auto-fetch 90 days of historical news for the new ticker (background)
        def fetch_news_background(t: str):
            if not _agent_enabled("news_tab"):
                return
            try:
                from agents.news_tab_agent import get_news_tab_agent
                agent = get_news_tab_agent()
                res = agent.fetch_historical_news(t, days=90)
                logger.info(f"Auto-fetched 90d news for {t}: {res.get('articles_added', 0)} articles added")
            except Exception as e:
                logger.error(f"Failed to auto-fetch news for {t}: {e}")
        
        threading.Thread(target=refresh_calendar_background, args=(ticker,), daemon=True).start()
        threading.Thread(target=fetch_news_background, args=(ticker,), daemon=True).start()
        _endpoint_payload_clear("watchlist_state")
        _endpoint_payload_clear_prefix("api_state:")
             
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding ticker: {e}", exc_info=True)
        raise _safe_error()

class TickerSettingsRequest(BaseModel):
    ticker: str
    settings: dict

@app.get("/api/watchlist/settings/{ticker}")
def api_get_ticker_settings(ticker: str):
    """Get settings for a specific ticker"""
    try:
        from api.actions_service import get_ticker_settings
        return get_ticker_settings(ticker)
    except Exception as e:
        raise _safe_error()

@app.post("/api/watchlist/settings")
def api_update_ticker_settings(request: TickerSettingsRequest):
    """Update settings for a specific ticker"""
    try:
        from api.actions_service import update_ticker_settings
        result = update_ticker_settings(request.ticker, request.settings)
        return result
    except Exception as e:
        raise _safe_error()


@app.post("/api/watchlist/settings/global/preview")
def api_preview_global_settings(request: ConfigUpdateRequest):
    """Preview impact before applying global ticker settings."""
    try:
        return {"status": "ok", "preview": preview_global_settings_apply(request.config or {})}
    except Exception as e:
        logger.error(f"Error previewing global settings: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/watchlist/settings/global")
def api_update_global_settings(request: ConfigUpdateRequest):
    """Apply same settings to ALL watchlist tickers"""
    try:
        from api.actions_service import update_ticker_settings
        from core.config import ConfigManager

        config = ConfigManager()
        wl = config.watchlist

        # Collect all tickers from watchlist
        all_tickers = []
        all_tickers.extend(wl.get("order", []))
        all_tickers.extend(wl.get("binance", []))
        all_tickers.extend(wl.get("yahoo", []))
        all_tickers = list(dict.fromkeys([str(t).upper().strip() for t in all_tickers if str(t).strip()]))

        # Apply settings to each ticker
        settings = request.config or {}
        preview = preview_global_settings_apply(settings)
        updated_count = 0
        # Tickers whose settings could not be written. They used to be logged
        # and dropped, and the route answered "ok" for all of them.
        failed: List[Dict[str, str]] = []

        for ticker in all_tickers:
            try:
                update_ticker_settings(ticker, settings)
                updated_count += 1
            except Exception as e:
                logger.error(f"Failed to update settings for {ticker}: {e}")
                failed.append({"ticker": ticker, "error": type(e).__name__})

        # Activity log entry in state.json for audit trail.
        try:
            state_payload = load_state_payload({})
            if not isinstance(state_payload, dict):
                state_payload = {}
            activity = state_payload.get("activity_log", []) or []
            activity.insert(0, {
                "time": datetime.now(timezone.utc).isoformat(),
                "type": "WATCHLIST",
                "subtype": "GLOBAL_SETTINGS",
                "desc": (
                    f"Applied global settings to {updated_count} of {len(all_tickers)} tickers"
                    + (f" ({len(failed)} failed)" if failed else "")
                    + f". Fields: {', '.join(preview.get('changed_fields', [])) or 'none'}"
                ),
            })
            state_payload["activity_log"] = activity[:300]
            save_state_payload(state_payload)
        except Exception as e:
            logger.warning(f"Could not append watchlist global settings activity: {e}")

        if failed:
            # Not "ok": the caller checks `status`/`ok` and shows `detail`.
            # 207 = some tickers were updated, 500 = none were.
            failed_names = ", ".join(item["ticker"] for item in failed[:10])
            if len(failed) > 10:
                failed_names += f" and {len(failed) - 10} more"
            return JSONResponse(
                status_code=207 if updated_count else 500,
                content={
                    "status": "partial" if updated_count else "error",
                    "ok": False,
                    "tickers_updated": updated_count,
                    "total_tickers": len(all_tickers),
                    "failed": failed,
                    "detail": (
                        f"Settings were applied to {updated_count} of {len(all_tickers)} tickers. "
                        f"NOT applied to: {failed_names}."
                    ),
                    "preview": preview,
                },
            )
        return {
            "status": "ok",
            "ok": True,
            "tickers_updated": updated_count,
            "total_tickers": len(all_tickers),
            "failed": [],
            "preview": preview,
        }
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        logger.error(f"FATAL ERROR in api_update_global_settings: {e}\n{error_details}", exc_info=True)
        raise _safe_error()

@app.delete("/api/tickers")
def api_remove_tickers(request: TickersDeleteRequest):
    """Remove tickers from watchlist"""
    try:
        tickers = [t.strip().upper() for t in request.tickers if t.strip()]
        if not tickers:
            raise HTTPException(status_code=400, detail="No tickers provided")
        result = remove_tickers(tickers, return_state=bool(request.return_state))

        # Trigger calendar refresh after removal in background to keep API responsive.
        def _refresh_calendar_after_removal() -> None:
            try:
                from agents.economic_calendar_agent import get_economic_calendar_agent
                get_economic_calendar_agent().refresh_data()
            except Exception as e:
                logger.warning(f"Calendar refresh after ticker removal failed: {e}")

        threading.Thread(target=_refresh_calendar_after_removal, daemon=True).start()
        # A removed ticker also leaves the hot list.
        _invalidate_hotlist_caches()

        # Same partial-failure shape as /api/watchlist/settings/global:
        # `failed: [{ticker, error}]`, `ok`, `status` ok|partial|error, `detail`.
        # A requested ticker that was not in the watchlist used to be dropped
        # silently and the UI announced "N tickers removed".
        if isinstance(result, dict) and isinstance(result.get("removed"), list):
            from api.actions_service import _normalize_input_ticker

            removed_set = {str(t or "").upper().strip() for t in result["removed"]}
            requested = list(dict.fromkeys(
                t for t in (_normalize_input_ticker(raw) for raw in tickers) if t
            ))
            failed = [
                {"ticker": t, "error": "not_in_watchlist"}
                for t in requested if t not in removed_set
            ]
            result["failed"] = failed
            result["ok"] = not failed
            result["tickers_removed"] = len(removed_set)
            result["total_tickers"] = len(requested)
            if failed:
                failed_names = ", ".join(item["ticker"] for item in failed[:10])
                if len(failed) > 10:
                    failed_names += f" and {len(failed) - 10} more"
                result["status"] = "partial" if removed_set else "error"
                result["detail"] = (
                    f"Removed {len(removed_set)} of {len(requested)} tickers. "
                    f"NOT removed (not in the Watchlist): {failed_names}."
                )
                return JSONResponse(status_code=207 if removed_set else 404, content=result)

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing tickers: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/tickers/reorder")
def api_reorder_tickers(request: TickersReorderRequest):
    """Reorder tickers in watchlist"""
    try:
        if not request.tickers:
            raise HTTPException(status_code=400, detail="No tickers provided")
        
        from api.actions_service import reorder_watchlist
        result = reorder_watchlist(request.tickers)
        _endpoint_payload_clear("watchlist_state")
        return result
    except Exception as e:
        logger.error(f"Error reordering tickers: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/watchlist/hotlist/toggle")
def api_toggle_hot_list(request: TickerRequest):
    """Add/remove ticker from hot list"""
    try:
        ticker = str(normalize_market_ticker(request.ticker) or "").upper().strip()
        if not ticker:
            raise HTTPException(status_code=400, detail="Ticker cannot be empty")

        from api.state_service import get_shared_config
        config = get_shared_config()
        is_in = config.toggle_hot_list(ticker)
        # Clears watchlist_state / hotlist_state / api_state:* and the heat
        # snapshot, and stops an in-flight pre-toggle build from re-caching.
        _invalidate_hotlist_caches()
        return {"status": "ok", "ticker": ticker, "is_in": is_in}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling hot list for {request.ticker}: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/analyze-ticker")
def api_analyze_ticker(request: AnalyzeRequest):
    """Analyze a specific ticker"""
    try:
        ticker = str(normalize_market_ticker(request.ticker) or "").upper().strip()
        if not ticker:
            raise HTTPException(status_code=400, detail="Ticker cannot be empty")
        logger.info(
            "API analyze-ticker request: %s (include_heavy=%s, disable_ollama=%s, force_refresh=%s)",
            ticker,
            request.include_heavy,
            request.disable_ollama,
            request.force_refresh
        )
        result = analyze_ticker_mode(
            ticker=ticker,
            include_heavy=request.include_heavy,
            disable_ollama=request.disable_ollama,
            force_refresh=bool(request.force_refresh),
            mode=request.mode,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error analyzing ticker: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/analyze")
def api_analyze(request: AnalyzeRequest):
    """Backward-compatible analyze endpoint (alias for analyze-ticker)."""
    return api_analyze_ticker(request)


@app.get("/api/strategy/config")
def api_get_strategy_config():
    """Get V2 strategy engine configuration."""
    try:
        return {"status": "ok", "config": get_strategy_config()}
    except Exception as e:
        logger.error(f"Error getting strategy config: {e}", exc_info=True)
        raise _safe_error()


@app.put("/api/strategy/config")
def api_put_strategy_config(request: StrategyConfigRequest):
    """Update V2 strategy engine configuration (partial)."""
    try:
        saved = save_strategy_config(request.config or {})
        return {"status": "ok", "config": saved}
    except Exception as e:
        logger.error(f"Error saving strategy config: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/strategy/performance")
def api_get_strategy_performance(symbol: Optional[str] = None):
    """Get cached V2 strategy performance snapshots."""
    try:
        sym = symbol.strip().upper() if symbol else None
        return get_strategy_performance(symbol=sym)
    except Exception as e:
        logger.error(f"Error getting strategy performance: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/watchlist/quick-ai")
def api_watchlist_quick_ai(request: QuickAIRequest):
    """On-demand quick AI decision for a watchlist ticker."""
    try:
        ticker = str(normalize_market_ticker(request.ticker) or "").upper().strip()
        if not ticker:
            raise HTTPException(status_code=400, detail="Ticker cannot be empty")
        return quick_analyze_ticker(ticker, force_refresh=bool(request.force_refresh))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Quick AI error for {request.ticker}: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/watchlist/pulse")
def api_watchlist_pulse():
    """Return news pulse snapshot for all current watchlist tickers."""
    try:
        from core.config import ConfigManager
        from core.news_pulse import build_news_pulse_bulk

        config = ConfigManager()
        wl = config.watchlist or {}
        tickers = []
        tickers.extend(wl.get("order", []))
        tickers.extend(wl.get("yahoo", []))
        tickers.extend(wl.get("binance", []))
        tickers = list(dict.fromkeys([str(t).upper().strip() for t in tickers if str(t).strip()]))
        ttl = int((config.news_pulse or {}).get("ttl_seconds", 45))
        pulses = build_news_pulse_bulk(tickers, ttl_seconds=ttl)
        return {"status": "ok", "tickers": tickers, "pulse": pulses, "ttl_seconds": ttl}
    except Exception as e:
        logger.error(f"Watchlist pulse error: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/signals/recalculate-backtest")
def api_recalculate_backtest():
    """Recalculate backtest results for all open signals"""
    try:
        from api.actions_service import recalculate_backtest_for_open_signals
        result = recalculate_backtest_for_open_signals()
        if result.get("success"):
            return {"status": "ok", "updated": result.get("updated", 0)}
        else:
            raise HTTPException(status_code=500, detail=result.get("error", "Unknown error"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error recalculating backtest: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/watchlist")
def api_get_watchlist():
    """Get watchlist tickers"""
    cache_key = "watchlist_state"
    try:
        cached = _endpoint_payload_get(cache_key, ttl_seconds=2.0)
        if cached is not None:
            cache_manager.record_manual_decision(
                key="watchlist-state",
                decision="HIT",
                reason="endpoint_payload_cache",
                source="watchlist_state_endpoint",
                ttl_seconds=2,
                age_seconds=None,
            )
            return Response(content=cached, media_type="application/json")

        from core.config import WATCHLIST_PATH
        watchlist = {"stocks": [], "crypto": []}
        read_error = None
        if WATCHLIST_PATH.exists():
            for _ in range(4):
                try:
                    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
                        watchlist = json.load(f)
                    read_error = None
                    break
                except Exception as e:
                    read_error = e
                    time.sleep(0.05)

        if read_error is not None and watchlist == {"stocks": [], "crypto": []}:
            stale = _endpoint_payload_peek(cache_key)
            if stale is not None:
                cache_manager.record_manual_decision(
                    key="watchlist-state",
                    decision="HIT",
                    reason="stale_payload_after_read_error",
                    source="watchlist_state_endpoint",
                    ttl_seconds=2,
                    age_seconds=None,
                )
                return Response(content=stale, media_type="application/json")
            raise read_error

        payload = json.dumps(watchlist, ensure_ascii=False, default=str).encode("utf-8")
        _endpoint_payload_set(cache_key, payload)
        return Response(content=payload, media_type="application/json")
    except Exception as e:
        logger.error(f"Error getting watchlist: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/backtest/run")
def api_run_backtest(request: BacktestRequest):
    """Run backtest"""
    try:
        result = run_backtest(
            # `or 30` turned an explicit 0 into a 30-day run; only a missing
            # value takes the default, anything else is validated downstream.
            days=30 if request.days is None else request.days,
            risk_mode=request.risk_mode or "MEDIUM",
            trade_mode=request.trade_mode or "PULLBACK",
            tickers=request.tickers,
            compare_all=request.compare_all,
            orb_entry_type=request.orb_entry_type or "MIDPOINT",
            variant_mode=request.variant_mode or "STRICT",
            **({} if request.custom_tp_pct is None else {"custom_tp_pct": request.custom_tp_pct}),
            **({} if request.custom_sl_pct is None else {"custom_sl_pct": request.custom_sl_pct})
        )
        return result
    except Exception as e:
        logger.error(f"Error running backtest: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/backtest/walk-forward")
def api_walk_forward_backtest(request: WalkForwardRequest):
    """Run walk-forward backtest"""
    try:
        from api.actions_service import run_walk_forward_backtest
        return run_walk_forward_backtest(
            ticker=request.ticker,
            days=request.days,
            train_days=request.train_days,
            test_days=request.test_days,
            risk_modes=request.risk_modes,
            trade_modes=request.trade_modes,
            custom_tp_pct=request.custom_tp_pct,
            custom_sl_pct=request.custom_sl_pct,
            max_windows=request.max_windows,
            variant_mode=request.variant_mode or "STRICT"
        )
    except Exception as e:
        logger.error(f"Error running walk-forward backtest: {e}", exc_info=True)
        raise _safe_error()

@app.get("/api/backtest/history")
def api_get_backtest_history(limit: Optional[int] = None, include_trades: bool = True):
    """Get backtest history (limit + include_trades bounds the payload)."""
    try:
        from api.actions_service import get_backtest_history
        return get_backtest_history(limit=limit, include_trades=include_trades)
    except Exception as e:
        logger.error(f"Error getting backtest history: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/backtest/oos-validation/latest")
async def api_get_latest_oos_validation():
    """Expose the latest reproducible price-only OOS audit per ticker.

    This endpoint is deliberately read-only and never treats an OOS artifact
    as approval for autonomous execution; that still needs live news and
    realized-capital guards.
    """
    try:
        from core.oos_validation_reports import get_latest_oos_validation_reports

        return get_latest_oos_validation_reports()
    except Exception as e:
        logger.error(f"Error getting OOS validation reports: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/backtest/research-validation/latest")
async def api_get_latest_research_validation():
    """Expose reproducible, pre-registered research artifacts read-only."""
    try:
        from core.research_validation_reports import get_latest_crypto_research_validation_reports

        return get_latest_crypto_research_validation_reports()
    except Exception as e:
        logger.error(f"Error getting research validation reports: {e}", exc_info=True)
        raise _safe_error()

@app.delete("/api/debug/signals")
def api_debug_clear_signals():
    """Debug endpoint to clear all signals from memory and disk"""
    if not IS_TEST_MODE:
        raise HTTPException(status_code=403, detail="Debug endpoints disabled in production")
    try:
        # Build suppression entries for current open signals so they don't immediately reappear.
        signals_data = load_signals_payload({"open": {}, "closed": []})
        open_signals = []
        if isinstance(signals_data, dict):
            raw_open = signals_data.get("open", {})
            if isinstance(raw_open, dict):
                open_signals = [v for v in raw_open.values() if isinstance(v, dict)]

        suppress_minutes = 60
        try:
            cfg_data = get_config() or {}
            suppress_minutes = int(
                (cfg_data.get("signal_settings", {}) or {}).get("manual_delete_suppress_minutes", 60) or 60
            )
        except Exception:
            suppress_minutes = 60
        suppress_minutes = max(1, suppress_minutes)

        suppressed_added = 0
        if open_signals:
            from core.config import SUPPRESSED_TICKERS_PATH
            suppressed_path = SUPPRESSED_TICKERS_PATH
            suppressed = {}
            if suppressed_path.exists():
                try:
                    with open(suppressed_path, "r", encoding="utf-8") as f:
                        suppressed = json.load(f)
                except Exception:
                    suppressed = {}

            now_iso = datetime.now(timezone.utc).astimezone().isoformat()
            for sig in open_signals:
                ticker = str(sig.get("ticker") or "").upper().strip()
                action = str(sig.get("action") or sig.get("type") or "").upper().strip()
                if not ticker or not action:
                    continue
                signal_mode = str(sig.get("signal_mode") or "UNKNOWN").upper().strip()
                variant_mode = str(sig.get("variant_mode") or "STRICT").upper().strip()
                suppress_key = f"{ticker}_{action}_{signal_mode}_{variant_mode}"
                suppressed[suppress_key] = {
                    "ticker": ticker,
                    "action": action,
                    "signal_mode": signal_mode,
                    "variant_mode": variant_mode,
                    "suppressed_at": now_iso,
                    "suppress_minutes": suppress_minutes,
                }
                suppressed_added += 1

            _atomic_json_write(Path(suppressed_path), suppressed)

        from api.engine_service import get_engine
        engine = get_engine()
        if engine and hasattr(engine, 'signal_manager'):
            engine.signal_manager.clear_all()
            return {
                "status": "ok",
                "message": "All signals cleared",
                "suppressed_added": suppressed_added,
                "suppress_minutes": suppress_minutes,
            }
        else:
            # Fallback: try to manually load and clear if engine not running?
            # But usually engine is running.
            # If engine is None, try to simply overwrite the file
            from core.signals import get_signal_manager
            sm = get_signal_manager()
            sm.clear_all()
            return {
                "status": "ok",
                "message": "Signals cleared (engine not active)",
                "suppressed_added": suppressed_added,
                "suppress_minutes": suppress_minutes,
            }
    except Exception as e:
        logger.error(f"Error clearing signals: {e}", exc_info=True)
        raise _safe_error()

# --- Watchlist sparkline cache: intraday close series, cache-first + bg-warmed ---
_sparkline_cache: dict = {}
_sparkline_lock = threading.Lock()
_sparkline_inflight: set = set()
_SPARKLINE_TTL = 900.0  # 15 min

# Chart payloads are intentionally cached separately from sparklines.  They are
# richer (OHLCV + annotations) and must never trigger a remote request on every
# paint of the frontend canvas.
_chart_cache: dict = {}
_chart_cache_lock = threading.Lock()
_chart_refresh_inflight: set[str] = set()
_CHART_TTL_BY_TIMEFRAME = {"1m": 15.0, "5m": 20.0, "15m": 30.0, "30m": 45.0, "1h": 60.0, "4h": 120.0, "1d": 300.0}
_CHART_TIMEFRAMES = frozenset(_CHART_TTL_BY_TIMEFRAME)
_CHART_MAX_BARS = max(80, min(350, int(os.getenv("MS_CHART_MAX_BARS", "240") or 240)))
_CHART_CACHE_MAX_ENTRIES = max(8, min(256, int(os.getenv("MS_CHART_CACHE_MAX_ENTRIES", "64") or 64)))


def _chart_store_key(cache_key: str) -> str:
    return f"chart_payload:{cache_key}"


def _remember_chart_payload(
    cache_key: str,
    payload: dict,
    *,
    created_at: Optional[float] = None,
    persist: bool = True,
) -> dict:
    timestamp = float(created_at or time.time())
    entry = {"created_at": timestamp, "payload": payload}
    with _chart_cache_lock:
        _chart_cache[cache_key] = entry
        if len(_chart_cache) > _CHART_CACHE_MAX_ENTRIES:
            oldest_keys = sorted(
                _chart_cache,
                key=lambda key: float((_chart_cache.get(key) or {}).get("created_at") or 0.0),
            )
            for old_key in oldest_keys[: len(_chart_cache) - _CHART_CACHE_MAX_ENTRIES]:
                _chart_cache.pop(old_key, None)
    if persist and "PYTEST_CURRENT_TEST" not in os.environ:
        try:
            from core.sqlite_store import get_sqlite_store

            get_sqlite_store().set_json(_chart_store_key(cache_key), entry)
        except Exception as exc:
            logger.debug("persistent chart cache write failed for %s: %s", cache_key, exc)
    return entry


def _load_persisted_chart_payload(cache_key: str) -> Optional[dict]:
    try:
        from core.sqlite_store import get_sqlite_store

        entry = get_sqlite_store().get_json(_chart_store_key(cache_key), None)
        if not isinstance(entry, dict) or not isinstance(entry.get("payload"), dict):
            return None
        created_at = float(entry.get("created_at") or 0.0)
        if created_at <= 0:
            return None
        return _remember_chart_payload(
            cache_key,
            entry["payload"],
            created_at=created_at,
            persist=False,
        )
    except Exception as exc:
        logger.debug("persistent chart cache read failed for %s: %s", cache_key, exc)
        return None


def _schedule_chart_refresh(cache_key: str, symbol: str, timeframe: str) -> bool:
    with _chart_cache_lock:
        if cache_key in _chart_refresh_inflight:
            return False
        _chart_refresh_inflight.add(cache_key)

    def _refresh() -> None:
        try:
            payload = _build_chart_payload(symbol, timeframe)
            if payload.get("available"):
                _remember_chart_payload(cache_key, payload)
            else:
                logger.warning("chart refresh returned no bars for %s", cache_key)
        except Exception as exc:
            logger.warning("background chart refresh failed for %s: %s", cache_key, exc)
        finally:
            with _chart_cache_lock:
                _chart_refresh_inflight.discard(cache_key)

    threading.Thread(
        target=_refresh,
        daemon=True,
        name=f"ChartRefresh-{symbol}-{timeframe}",
    ).start()
    return True


def _normalise_chart_frame(frame):
    """Return the normalized lower-case OHLCV frame used by analysis engines."""
    import pandas as pd

    if frame is None or frame.empty:
        return None
    rename_map = {column: str(column).lower() for column in frame.columns}
    normalized = frame.rename(columns=rename_map).copy()
    required = ["open", "high", "low", "close"]
    if not all(column in normalized.columns for column in required):
        return None
    if "volume" not in normalized.columns:
        normalized["volume"] = 0.0
    for column in [*required, "volume"]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized = normalized.dropna(subset=required)
    return normalized if not normalized.empty else None


def _is_crypto_chart_symbol(symbol: str) -> bool:
    normalized = str(normalize_market_ticker(symbol) or "").upper().strip()
    return bool(normalized and is_crypto_ticker(normalized))


def _fetch_yahoo_chart_frame(symbol: str, period: str, interval: str):
    """Fetch Yahoo chart candles through the bounded direct endpoint."""
    import pandas as pd
    from core.http_utils import http_get

    response = http_get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}" ,
        provider="yahoo",
        params={
            "range": period,
            "interval": interval,
            "includePrePost": "false",
            "events": "div,splits",
        },
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=(3, 8),
        max_retries=0,
    )
    if response is None or response.status_code != 200:
        return None
    payload = response.json() if response.content else {}
    result = (((payload.get("chart") or {}).get("result") or [None])[0] or {})
    timestamps = result.get("timestamp") if isinstance(result.get("timestamp"), list) else []
    quote_rows = (((result.get("indicators") or {}).get("quote") or [{}])[0] or {})
    if not timestamps or not quote_rows:
        return None

    def column(name: str, default=0.0):
        values = quote_rows.get(name) if isinstance(quote_rows.get(name), list) else []
        return [(values[index] if index < len(values) and values[index] is not None else default) for index in range(len(timestamps))]

    frame = pd.DataFrame(
        {
            "open": column("open", None),
            "high": column("high", None),
            "low": column("low", None),
            "close": column("close", None),
            "volume": column("volume", 0.0),
        },
        index=pd.to_datetime(timestamps, unit="s", utc=True),
    )
    return _normalise_chart_frame(frame)


def _fetch_chart_frame(symbol: str, timeframe: str):
    """Fetch a bounded OHLCV chart from the same providers as the live system."""
    if _is_crypto_chart_symbol(symbol):
        from api.market_data_service import BinanceClient, KuCoinClient

        kucoin_interval = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1hour", "4h": "4hour", "1d": "1day"}[timeframe]
        for client, interval in ((BinanceClient, timeframe), (KuCoinClient, kucoin_interval)):
            try:
                frame = client.fetch_klines(symbol, interval=interval, limit=350, ttl=int(_CHART_TTL_BY_TIMEFRAME[timeframe]))
                normalized = _normalise_chart_frame(frame)
                if normalized is not None and len(normalized) >= 20:
                    return normalized, client.__name__.replace("Client", "").lower()
            except Exception as exc:
                logger.debug("chart OHLCV fetch failed for %s from %s: %s", symbol, client.__name__, exc)
        return None, "unavailable"

    interval_map = {"1m": ("1d", "1m"), "5m": ("5d", "5m"), "15m": ("5d", "15m"), "30m": ("1mo", "30m"), "1h": ("1mo", "1h"), "4h": ("3mo", "1h"), "1d": ("1y", "1d")}
    period, interval = interval_map[timeframe]
    try:
        frame = _fetch_yahoo_chart_frame(symbol, period, interval)
        normalized = _normalise_chart_frame(frame)
        if timeframe == "4h" and normalized is not None:
            normalized = _normalise_chart_frame(normalized.resample("4h").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum",
            }).dropna(subset=["open", "high", "low", "close"]))
        if normalized is not None and len(normalized) >= 20:
            return normalized, "yahoo_chart"
    except Exception as exc:
        logger.debug("stock chart OHLCV fetch failed for %s: %s", symbol, exc)
    return None, "unavailable"


def _build_chart_payload(symbol: str, timeframe: str) -> dict:
    from core.chart_replay import build_replay_states
    from core.chart_patterns import analyze_chart_patterns
    from core.liquidity_engine import analyze_liquidity
    from core.market_structure import analyze_market_structure
    from core.supertrend import analyze_supertrend
    from core.volume_structure import analyze_volume_structure

    frame, source = _fetch_chart_frame(symbol, timeframe)
    asset_class = (
        "CRYPTO" if _is_crypto_chart_symbol(symbol)
        else ("COMMODITY" if "=F" in symbol else "STOCK")
    )
    if frame is None:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": [],
            "source": source,
            "available": False,
            "asset_class": asset_class,
        }
    analysis_frame = frame.tail(_CHART_MAX_BARS).copy()
    bars = []
    for index, row in analysis_frame.iterrows():
        timestamp = index.timestamp() if hasattr(index, "timestamp") else None
        if timestamp is None:
            continue
        bars.append({
            "time": int(timestamp), "open": round(float(row.open), 8), "high": round(float(row.high), 8),
            "low": round(float(row.low), 8), "close": round(float(row.close), 8), "volume": round(float(row.volume), 8),
        })
    structure = analyze_market_structure(analysis_frame)
    liquidity = analyze_liquidity(analysis_frame)
    patterns = analyze_chart_patterns(analysis_frame)
    volume = analyze_volume_structure(analysis_frame)
    supertrend = analyze_supertrend(analysis_frame, period=10, multiplier=3.0)
    annotations = {
        "pivots": structure.get("pivots", []),
        "structure_events": structure.get("structure_events", []),
        "liquidity_pools": liquidity.get("liquidity_pools", []),
        "liquidity_events": liquidity.get("liquidity_events", []),
        "chart_patterns": patterns.get("chart_patterns", []),
        "volume_events": volume.get("volume_events", []),
        "supertrend_events": supertrend.get("supertrend_events", []),
    }
    annotation_keys = {
        "pivots", "structure_events", "liquidity_pools", "liquidity_events",
        "chart_patterns", "volume_events", "supertrend_points", "supertrend_events",
    }
    latest_state = {
        key: value
        for key, value in {**structure, **liquidity, **patterns, **volume, **supertrend}.items()
        if key not in annotation_keys
    }
    return {
        "symbol": symbol, "timeframe": timeframe, "bars": bars, "source": source, "available": bool(bars),
        "asset_class": asset_class,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "state": latest_state,
        "annotations": annotations,
        "indicators": {"supertrend": supertrend.get("supertrend_points", [])},
        "replay_states": build_replay_states(analysis_frame),
    }


@app.get("/api/chart/{symbol}")
def api_chart(symbol: str, timeframe: str = "5m", refresh: bool = False, replay_index: Optional[int] = None):
    """OHLCV plus causal annotations; ``replay_index`` exposes no future bars."""
    from core.chart_replay import apply_visual_replay
    normalized_symbol = str(normalize_market_ticker(symbol) or "").upper().strip()
    normalized_timeframe = str(timeframe or "5m").lower().strip()
    if not normalized_symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if normalized_timeframe not in _CHART_TIMEFRAMES:
        raise HTTPException(status_code=400, detail=f"unsupported timeframe: {normalized_timeframe}")
    cache_key = f"{normalized_symbol}:{normalized_timeframe}"
    now = time.time()
    with _chart_cache_lock:
        cached = _chart_cache.get(cache_key)
    if cached is None and not refresh:
        cached = _load_persisted_chart_payload(cache_key)
    if cached and not refresh:
        cache_age = max(0.0, now - float(cached.get("created_at") or 0.0))
        if cache_age < _CHART_TTL_BY_TIMEFRAME[normalized_timeframe]:
            payload = {**cached["payload"], "cache": "hit"}
            return apply_visual_replay(payload, replay_index) if replay_index is not None else payload
        _schedule_chart_refresh(
            cache_key,
            normalized_symbol,
            normalized_timeframe,
        )
        payload = {
            **cached["payload"],
            "cache": "stale",
            "refreshing": True,
            "cache_age_seconds": round(cache_age, 3),
        }
        return apply_visual_replay(payload, replay_index) if replay_index is not None else payload
    payload = _build_chart_payload(normalized_symbol, normalized_timeframe)
    _remember_chart_payload(cache_key, payload)
    response = {**payload, "cache": "miss"}
    return apply_visual_replay(response, replay_index) if replay_index is not None else response


@app.get("/api/entry-chart/{snapshot_id}")
def api_entry_chart(snapshot_id: str):
    """Serve one immutable chart captured at a paper-position entry."""
    from core.entry_chart_snapshot import resolve_entry_chart_path

    path = resolve_entry_chart_path(snapshot_id)
    if path is None:
        raise HTTPException(status_code=404, detail="entry chart snapshot not found")
    return FileResponse(
        path,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _fetch_sparkline_points(symbol: str):
    """Return ~30 recent intraday closes from the instrument's real venue.

    Crypto uses Binance first and KuCoin second.  It must never fall through to
    Yahoo Finance: Yahoo cannot resolve many exchange-native pairs and an empty
    or unrelated fallback would make the Watchlist look like it has a valid
    chart when it does not.
    """
    norm = str(symbol or "").upper().replace("-", "")
    is_crypto = norm.endswith("USDT") or norm.endswith("USDC") or norm.endswith("USD")
    if is_crypto:
        try:
            from api.market_data_service import (
                BinanceClient,
                KuCoinClient,
                prefers_kucoin_ticker,
            )

            venues = (
                ((KuCoinClient, "15min"), (BinanceClient, "15m"))
                if prefers_kucoin_ticker(symbol)
                else ((BinanceClient, "15m"), (KuCoinClient, "15min"))
            )
            for client, interval in venues:
                df = client.fetch_klines(symbol, interval=interval, limit=32)
                if df is None or df.empty:
                    continue
                close_column = next((column for column in ("Close", "close") if column in df.columns), None)
                if close_column is None:
                    continue
                vals = [float(x) for x in df[close_column].dropna().tolist() if x == x][-30:]
                if len(vals) >= 4:
                    return [round(v, 8) for v in vals]
        except Exception as e:
            logger.debug(f"crypto sparkline fetch failed {symbol}: {e}")
        return None
    try:
        from core.stock_data_client import StockDataClient
        df = StockDataClient().get_historical(symbol, period="1d", interval="15m")
        if df is not None and not df.empty and "Close" in df.columns:
            vals = [float(x) for x in df["Close"].dropna().tolist() if x == x]
            vals = vals[-30:]
            if len(vals) >= 4:
                return [round(v, 6) for v in vals]
    except Exception as e:
        logger.debug(f"sparkline fetch failed {symbol}: {e}")
    return None


# Symbols whose venue returned no usable candles, and when (negative cache).
_sparkline_miss: dict = {}
_SPARKLINE_MISS_TTL = 300.0


@app.get("/api/sparklines")
def api_sparklines(symbols: str = ""):
    """Intraday close series per ticker for watchlist sparklines (cache-first, bg-warmed)."""
    import time as _t
    try:
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:24]
        now = _t.time()
        out = {}
        missing = []
        with _sparkline_lock:
            for s in syms:
                c = _sparkline_cache.get(s)
                if c and (now - c["ts"]) < _SPARKLINE_TTL:
                    out[s] = c["points"]
                elif (now - _sparkline_miss.get(s, 0.0)) < _SPARKLINE_MISS_TTL:
                    # The venue had no candles for this symbol a moment ago.
                    # Without this every poll re-fetched it from the exchange.
                    continue
                else:
                    missing.append(s)
        ctx = get_request_context()
        forced_cache = bool(ctx and ctx.cache_only)
        if missing and not forced_cache:
            def _warm(to_fetch):
                for s in to_fetch:
                    with _sparkline_lock:
                        if s in _sparkline_inflight:
                            continue
                        _sparkline_inflight.add(s)
                    try:
                        pts = _fetch_sparkline_points(s)
                        with _sparkline_lock:
                            if pts:
                                _sparkline_cache[s] = {"points": pts, "ts": _t.time()}
                                _sparkline_miss.pop(s, None)
                            else:
                                if len(_sparkline_miss) > 2000:
                                    _sparkline_miss.clear()
                                _sparkline_miss[s] = _t.time()
                    finally:
                        with _sparkline_lock:
                            _sparkline_inflight.discard(s)
            threading.Thread(target=_warm, args=(missing[:24],), daemon=True, name="SparklineWarm").start()
        return {"sparklines": out}
    except Exception as e:
        logger.error(f"Error in /api/sparklines: {e}")
        return {"sparklines": {}}


@app.get("/api/live-prices")
def api_get_live_prices(
    remote: bool = False,
    symbols: str = "",
    cache_fallback: bool = True,
    max_age_seconds: int = 45,
):
    """
    Get prices from local single source (`state.json`) first.
    Remote fetch is optional (remote=true) and refreshes missing/stale symbols.
    `symbols` param allows requesting specific tickers (e.g. signal tickers not in watchlist).
    `cache_fallback=false` with `remote=true` returns only quotes fetched during this request.
    """
    try:
        import json
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from api.market_data_service import (
            BinanceClient,
            KuCoinClient,
            YahooClient,
            prefers_kucoin_ticker,
        )
        from core.alpaca_client import get_alpaca_client
        from core.config import BASE_DIR, is_crypto_ticker, normalize_ticker

        def _safe_float(v, default=0.0):
            try:
                f = float(str(v).replace("%", ""))
                return default if (f != f or f == float("inf") or f == float("-inf")) else f
            except Exception:
                return float(default)

        def _nullable_change_pct(raw) -> Optional[float]:
            """Return null when change data is genuinely absent (F9).
            A ticker with NO change data should NOT fabricate +0.00% as if
            real. The frontend already treats null as 'no change data'."""
            if raw is None or raw == "":
                return None
            try:
                f = float(str(raw).replace("%", ""))
                if f != f or f == float("inf") or f == float("-inf"):
                    return None
                return round(f, 2)
            except Exception:
                return None

        def _uses_yahoo_futures_price(ticker: str) -> bool:
            return "=F" in (normalize_ticker(ticker, provider="yahoo") or "")

        freshness_limit = max(5, min(int(max_age_seconds or 45), 3600))
        # Last trade older than 15 min => not a live price (closed session or
        # delayed feed); older than 4.5 days => no longer "the last close"
        # (covers a long weekend) but a stale/delisted quote.
        _MARKET_LIVE_WINDOW_SECONDS = 15 * 60
        _MARKET_STALE_SECONDS = 4.5 * 86400

        def _observed_at(value: Any) -> Optional[datetime]:
            if value in (None, ""):
                return None
            try:
                observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                return observed.astimezone(timezone.utc)
            except (TypeError, ValueError):
                return None

        def _quote_age_seconds(row: Optional[Dict[str, Any]]) -> Optional[float]:
            observed = _observed_at((row or {}).get("timestamp"))
            if observed is None:
                return None
            return max(0.0, (datetime.now(timezone.utc) - observed).total_seconds())

        def _prefer_quote(ticker: str, candidate: Dict[str, Any]) -> None:
            """Keep the newest verified quote instead of the first cache hit."""
            existing = prices.get(ticker)
            if not isinstance(existing, dict):
                prices[ticker] = candidate
                return
            existing_at = _observed_at(existing.get("timestamp"))
            candidate_at = _observed_at(candidate.get("timestamp"))
            if candidate_at is not None and (existing_at is None or candidate_at > existing_at):
                prices[ticker] = candidate
                return
            if candidate_at == existing_at and candidate.get("live") and not existing.get("live"):
                prices[ticker] = candidate

        _us_session_cache: Dict[str, Any] = {}

        def _us_equity_session_closed() -> bool:
            """True when the US equity market is outside its regular AND
            extended sessions (weekend, holiday, overnight). Unknown => False,
            so a calendar failure never relabels a quote."""
            if "closed" not in _us_session_cache:
                try:
                    from core.market_hours import us_equity_session_snapshot

                    state = str((us_equity_session_snapshot() or {}).get("state") or "").upper()
                    _us_session_cache["closed"] = state == "CLOSED"
                except Exception:
                    _us_session_cache["closed"] = False
            return bool(_us_session_cache["closed"])

        def _decorate_freshness(row: Dict[str, Any], ticker: str = "") -> Dict[str, Any]:
            age = _quote_age_seconds(row)
            if row.get("live") and (age is None or age <= freshness_limit):
                freshness = "LIVE"
            elif age is not None and age <= freshness_limit:
                freshness = "CACHED"
            else:
                freshness = "STALE"
            # A quote we fetched a second ago is not "live" when the exchange
            # last traded it on Friday (or months ago for a delisted name).
            # market_time is the provider's own last-trade time; when it is
            # known it decides the badge, not our fetch time.
            market_at = _observed_at(row.get("market_time"))
            market_age = None
            if market_at is not None:
                market_age = max(0.0, (datetime.now(timezone.utc) - market_at).total_seconds())
                if market_age > _MARKET_LIVE_WINDOW_SECONDS:
                    row["live"] = False
                    session_known_open = (
                        row.get("market_open") is True and age is not None and age <= 120
                    )
                    if market_age > _MARKET_STALE_SECONDS:
                        freshness = "STALE"
                        # A months-old last trade has no meaningful day change.
                        row["change_pct"] = None
                    elif session_known_open and market_age <= 4 * 3600:
                        freshness = "DELAYED"
                    else:
                        freshness = "CLOSED"
            elif (
                freshness in ("LIVE", "CACHED")
                and ticker
                and not is_crypto_ticker(ticker)
                and ticker not in _crypto_watchlist_symbols
                and _us_equity_session_closed()
            ):
                # No exchange trade time (e.g. an eToro / alternate-source
                # quote): "we fetched it a second ago" proves nothing about
                # when it last traded. With the US market closed a stock quote
                # is the LAST quoted price, not a live one. Crypto trades 24/7.
                freshness = "LAST"
                row["live"] = False
            row["market_age_seconds"] = round(market_age, 3) if market_age is not None else None
            row["age_seconds"] = round(age, 3) if age is not None else None
            row["freshness"] = freshness
            return row

        def _carry_market_fields(target: Dict[str, Any], source: Any) -> Dict[str, Any]:
            """Copy the provider's market metadata onto a quote row."""
            if isinstance(source, dict):
                for key in ("market_time", "market_open", "currency", "exchange"):
                    value = source.get(key)
                    if value not in (None, ""):
                        target[key] = value
            return target

        # F12: cap the number of symbols processed per request so large
        # watchlists do not fan out into many serialized 3s remote timeouts
        # and stall the UI. Backward-compatible: frontend ignores unknown
        # fields. Configurable via MS_LIVE_PRICES_MAX_SYMBOLS (default 50).
        try:
            max_symbols = int(os.environ.get("MS_LIVE_PRICES_MAX_SYMBOLS", "50") or 50)
        except Exception:
            max_symbols = 50
        max_symbols = max(1, min(max_symbols, 200))

        # 1) Watchlist tickers
        try:
            from api.actions_service import _load_watchlist_payload
            watchlist = _load_watchlist_payload() or {"stocks": [], "crypto": []}
        except Exception:
            watchlist = {"stocks": [], "crypto": []}

        stock_tickers = list(watchlist.get("stocks", []) or watchlist.get("yahoo", []) or [])
        crypto_tickers = list(
            watchlist.get("crypto", [])
            or watchlist.get("cryptos", [])
            or watchlist.get("binance", [])
            or []
        )
        requested = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else []
        if requested:
            ordered = list(dict.fromkeys(requested))
        else:
            ordered = list(dict.fromkeys([str(t or "").upper().strip() for t in (stock_tickers + crypto_tickers) if str(t or "").strip()]))

        # F12: cap symbols processed per request to avoid large-watchlist
        # multi-second stalls (each missing ticker fans out to a 3s remote
        # timeout via ThreadPoolExecutor(max_workers=8)).
        requested_count = len(ordered)
        truncated = requested_count > max_symbols
        if truncated:
            ordered = ordered[:max_symbols]
        returned_count = len(ordered)

        prices = {}
        remote_allowed = os.getenv("MS_DISABLE_LIVE_PRICE_REMOTE", "0") != "1"
        strict_remote = bool(remote and not cache_fallback)
        fetched_at = datetime.now(timezone.utc).isoformat()

        # 2) Local single source: state.json. Strict remote mode intentionally
        # skips local values so callers do not paint stale app cache as live.
        cache_path = DATA_DIR / "live_prices_cache.json"
        if not strict_remote:
            state_tickers = {}
            try:
                state_payload = load_state_payload({})
                if isinstance(state_payload, dict):
                    raw = state_payload.get("tickers", {}) or {}
                    if isinstance(raw, dict):
                        state_tickers = raw
            except Exception:
                state_tickers = {}

            for ticker in ordered:
                row = state_tickers.get(ticker, {})
                if not isinstance(row, dict):
                    continue
                price = _safe_float(row.get("price"), 0.0)
                if price <= 0:
                    continue
                _prefer_quote(ticker, {
                    "price": price,
                    "change_pct": _nullable_change_pct(row.get("change_pct")),
                    "live": False,
                    "source": "state",
                    "timestamp": row.get("last_update") or row.get("timestamp") or "",
                })

            # 2b) Quotes verified by the Capital P&L tracker. This SQLite cache
            # is shared by the engine and API processes and avoids a remote
            # request merely to paint an already-monitored open position.
            try:
                from core.live_price_cache import get_live_prices

                now_utc = datetime.now(timezone.utc)
                for ticker, cp in get_live_prices(ordered).items():
                    if not isinstance(cp, dict):
                        continue
                    cprice = _safe_float(cp.get("price"), 0.0)
                    if cprice <= 0:
                        continue
                    timestamp = cp.get("timestamp") or ""
                    is_live = False
                    try:
                        observed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                        if observed.tzinfo is None:
                            observed = observed.replace(tzinfo=timezone.utc)
                        is_live = (now_utc - observed.astimezone(timezone.utc)).total_seconds() <= 45
                    except Exception:
                        pass
                    _prefer_quote(ticker, {
                        "price": cprice,
                        "change_pct": _nullable_change_pct(cp.get("change_pct")),
                        "live": is_live,
                        "source": cp.get("source") or "capital_price_cache",
                        "timestamp": timestamp,
                    })
            except Exception:
                pass

            # 3) Local persistent fallback cache
            if cache_path.exists():
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        cached_payload = json.load(f) or {}
                    cached_prices = cached_payload.get("prices", {}) or {}
                    if isinstance(cached_prices, dict):
                        for ticker in ordered:
                            cp = cached_prices.get(ticker)
                            if not isinstance(cp, dict):
                                continue
                            cprice = _safe_float(cp.get("price"), 0.0)
                            if cprice > 0:
                                _prefer_quote(ticker, _carry_market_fields({
                                    "price": cprice,
                                    "change_pct": _nullable_change_pct(cp.get("change_pct")),
                                    "live": False,
                                    "source": cp.get("source") or "live_prices_cache",
                                    "timestamp": cp.get("timestamp") or "",
                                }, cp))
                except Exception:
                    pass

        missing = [t for t in ordered if t not in prices]

        # 4) Remote fetch. This is explicit: remote=false must remain a bounded
        # cache-only read even when requested symbols are absent. Callers that
        # need an authoritative refresh already opt in with remote=true.
        should_remote = remote_allowed and remote
        if should_remote:
            crypto_set = set(str(x or "").upper().strip() for x in crypto_tickers)
            if strict_remote:
                refresh_targets = list(ordered)
            else:
                refresh_targets = [
                    ticker
                    for ticker in ordered
                    if ticker not in prices
                    or _quote_age_seconds(prices.get(ticker)) is None
                    or float(_quote_age_seconds(prices.get(ticker)) or 0.0) > freshness_limit
                ]
            missing_crypto = [t for t in refresh_targets if t in crypto_set or is_crypto_ticker(t)]
            missing_futures = [t for t in refresh_targets if t not in missing_crypto and _uses_yahoo_futures_price(t)]
            missing_stocks = [t for t in refresh_targets if t not in missing_crypto and t not in missing_futures]

            def fetch_crypto_price(ticker):
                price = 0.0
                change_pct = 0.0
                source = "unavailable"
                venues = (
                    (("kucoin", KuCoinClient), ("binance", BinanceClient))
                    if prefers_kucoin_ticker(ticker)
                    else (("binance", BinanceClient), ("kucoin", KuCoinClient))
                )
                for venue_name, venue_client in venues:
                    venue_price = venue_client.get_last_price(ticker, ttl=2) or 0.0
                    if venue_price <= 0:
                        continue
                    price = venue_price
                    change_pct = venue_client.get_24h_change(ticker, ttl=30)
                    source = venue_name
                    break
                if not price or price <= 0:
                    alpaca_data = get_alpaca_client().get_market_data(ticker, crypto=True, ttl=5)
                    if alpaca_data and float(alpaca_data.get("price") or 0.0) > 0:
                        price = float(alpaca_data["price"])
                        change_pct = float(alpaca_data.get("change_pct") or 0.0)
                        source = str(alpaca_data.get("source") or "alpaca_crypto")
                return ticker, price, change_pct, source

            if missing_crypto:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    futures = {executor.submit(fetch_crypto_price, t): t for t in missing_crypto}
                    for future in as_completed(futures):
                        try:
                            ticker, price, change_pct, source = future.result(timeout=3)
                            p = _safe_float(price, 0.0)
                            if p > 0:
                                _prefer_quote(ticker, {
                                    "price": p,
                                    "change_pct": _nullable_change_pct(change_pct),
                                    "live": True,
                                    "source": source,
                                    "timestamp": fetched_at,
                                })
                        except Exception:
                            pass

            def fetch_stock_price(ticker):
                if not _uses_yahoo_futures_price(ticker):
                    alpaca_data = get_alpaca_client().get_market_data(ticker, crypto=False, ttl=5)
                    if alpaca_data and float(alpaca_data.get("price") or 0.0) > 0:
                        return (
                            ticker,
                            _safe_float(alpaca_data["price"], 0.0),
                            _nullable_change_pct(alpaca_data.get("change_pct")),
                            str(alpaca_data.get("source") or "alpaca_iex"),
                            {"market_time": alpaca_data.get("timestamp"), "currency": "USD"},
                        )
                data = YahooClient.get_stock_data(ticker, ttl=2)
                p = _safe_float(data.get("price"), 0.0)
                # change_pct_known=False: the provider had no previous close, so
                # the 0.0 it returns is a placeholder, not a measured 0.00%.
                cp = None if data.get("change_pct_known") is False else _nullable_change_pct(data.get("change_pct"))
                if p > 0:
                    return ticker, p, cp, "yahoo", data
                if p <= 0:
                    try:
                        from core.etoro_client import get_etoro_client
                        et = get_etoro_client()
                        if et.is_available():
                            eq = et.get_quote(ticker)
                            if eq and eq.get("price", 0) > 0:
                                return ticker, eq["price"], eq.get("changesPercentage"), "etoro", {}
                    except Exception:
                        pass
                return ticker, p, cp, "yahoo", {}

            yahoo_targets = list(dict.fromkeys(missing_futures + missing_stocks))
            # One multi-symbol Yahoo request for the whole batch instead of a
            # yfinance fast_info + info round trip per symbol (the cause of the
            # 8-13 s watchlist batches). Used when Alpaca cannot serve the
            # symbol anyway; whatever it cannot price falls through to the
            # per-symbol path below.
            if yahoo_targets:
                try:
                    _alpaca_probe = getattr(get_alpaca_client(), "is_available", None)
                    alpaca_ready = bool(_alpaca_probe()) if callable(_alpaca_probe) else True
                except Exception:
                    alpaca_ready = True
                batch_targets = [
                    t for t in yahoo_targets
                    if not alpaca_ready or _uses_yahoo_futures_price(t)
                ]
                if batch_targets:
                    try:
                        snapshots = YahooClient.get_quote_snapshots(batch_targets, ttl=2)
                    except Exception:
                        snapshots = {}
                    for ticker, snap in (snapshots or {}).items():
                        p = _safe_float((snap or {}).get("price"), 0.0)
                        if p <= 0:
                            continue
                        _prefer_quote(ticker, _carry_market_fields({
                            "price": p,
                            "change_pct": _nullable_change_pct(snap.get("change_pct")),
                            "live": True,
                            "source": str(snap.get("source") or "yahoo"),
                            "timestamp": fetched_at,
                        }, snap))
                    yahoo_targets = [t for t in yahoo_targets if t not in (snapshots or {})]
            if yahoo_targets:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    futures = {executor.submit(fetch_stock_price, t): t for t in yahoo_targets}
                    for future in as_completed(futures):
                        try:
                            ticker, price, change_pct, source, quote_meta = future.result(timeout=3)
                            p = _safe_float(price, 0.0)
                            if p > 0:
                                _prefer_quote(ticker, _carry_market_fields({
                                    "price": p,
                                    "change_pct": _nullable_change_pct(change_pct),
                                    "live": True,
                                    "source": source,
                                    "timestamp": fetched_at,
                                }, quote_meta))
                        except Exception:
                            pass

        _crypto_watchlist_symbols = {
            str(x or "").upper().strip() for x in crypto_tickers if str(x or "").strip()
        }
        for _quote_ticker, quote in prices.items():
            if isinstance(quote, dict):
                quote["price"] = _safe_float(quote.get("price"), 0.0)
                quote["change_pct"] = _nullable_change_pct(quote.get("change_pct"))
                _decorate_freshness(quote, str(_quote_ticker or "").upper().strip())

        # 5) Persist local cache for reuse. Merge with existing symbols so a
        # one-symbol Capital request cannot erase the watchlist cache.
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            merged_prices = {}
            if cache_path.exists():
                try:
                    existing_payload = json.loads(cache_path.read_text(encoding="utf-8"))
                    existing_prices = existing_payload.get("prices", {}) if isinstance(existing_payload, dict) else {}
                    if isinstance(existing_prices, dict):
                        merged_prices.update(existing_prices)
                except Exception:
                    pass
            merged_prices.update(prices)
            _atomic_json_write(Path(cache_path), {"ts": time.time(), "prices": merged_prices})
        except Exception:
            pass

        source_label = "remote" if strict_remote and should_remote else ("state_cache" if not should_remote else "state_cache+remote")
        return {
            "prices": prices,
            "source": source_label,
            "truncated": truncated,
            "requested_count": requested_count,
            "returned_count": returned_count,
            "max_symbols": max_symbols,
        }
    except Exception as e:
        logger.error(f"Error in /api/live-prices: {e}")
        return {"prices": {}, "source": "error"}


@app.get("/api/alpaca/status", tags=["Market Data"])
def api_alpaca_status(include_clock: bool = False):
    """Alpaca configuration/health without exposing credential values."""
    from core.alpaca_client import get_alpaca_client

    client = get_alpaca_client()
    payload = client.status()
    payload["provider"] = "alpaca"
    if include_clock and payload["available"]:
        payload["clock"] = client.get_clock()
    return payload


@app.get("/api/alpaca/snapshots", tags=["Market Data"])
def api_alpaca_snapshots(symbols: str):
    """Normalized Alpaca snapshots for up to 50 stock/crypto symbols."""
    from core.alpaca_client import get_alpaca_client

    requested = list(
        dict.fromkeys(
            str(symbol or "").upper().strip()
            for symbol in str(symbols or "").split(",")
            if str(symbol or "").strip()
        )
    )
    if not requested:
        raise HTTPException(status_code=400, detail="symbols is required")
    if len(requested) > 50:
        raise HTTPException(status_code=400, detail="at most 50 symbols are allowed")

    client = get_alpaca_client()
    if not client.is_available():
        raise HTTPException(
            status_code=503,
            detail="Alpaca is not configured; set ALPACA_API_KEY and ALPACA_SECRET_KEY",
        )
    crypto = [ticker for ticker in requested if is_crypto_ticker(ticker)]
    stocks = [ticker for ticker in requested if ticker not in crypto]
    rows = {}
    if stocks:
        rows.update(client.get_market_data_batch(stocks, crypto=False))
    if crypto:
        rows.update(client.get_market_data_batch(crypto, crypto=True))
    return {
        "provider": "alpaca",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "snapshots": rows,
        "requested_count": len(requested),
        "returned_count": len(rows),
    }


@app.get("/api/crypto/movers", tags=["Scanner"])
def api_crypto_movers(limit: int = 5):
    """
    Get (USDT) Top Gainers & Losers from KuCoin.
    """
    try:
        if os.getenv("MS_DISABLE_SCANNER_AGENT", "0") == "1":
            return {"gainers": [], "losers": []}
        from api.market_data_service import get_crypto_movers
        # Bound the page size: the Scanner tab asks for 50 per side.
        limit = max(1, min(int(limit or 5), 100))
        return get_crypto_movers(limit=limit)
    except Exception as e:
        logger.error(f"Error fetching crypto movers: {e}")
        # Same shape as KuCoinClient.get_top_movers on failure: an empty list
        # without `available: False` reads as "no pair moved today".
        return {"gainers": [], "losers": [], "available": False, "stale": False,
                "reason": "KuCoin movers unavailable (server error)"}


# ==================== NEW AGENTS API ENDPOINTS ====================

# --- News Tab Agent ---
_NEWS_TAB_STATE_CACHE_LOCK = threading.Lock()
_NEWS_TAB_STATE_CACHE = {"key": None, "ts": 0.0, "data": None, "payload": None}
_NEWS_TAB_STATE_INFLIGHT = {}
_NEWS_TAB_REQ_CACHE_LOCK = threading.Lock()
_NEWS_TAB_REQ_CACHE: Dict[str, Any] = {
    "ts": 0.0,
    "active_tickers": [],
    "news_enabled": True,
    "ttl_seconds": 10.0,
    "config": {},
}
_CALENDAR_EVENTS_CACHE_LOCK = threading.Lock()
_CALENDAR_EVENTS_CACHE: Dict[str, Any] = {"key": None, "ts": 0.0, "payload": None}
_ENDPOINT_PAYLOAD_CACHE_LOCK = threading.Lock()
_ENDPOINT_PAYLOAD_CACHE: Dict[str, Dict[str, Any]] = {}
_ENDPOINT_PAYLOAD_CACHE_MAX = 200
_ENDPOINT_PAYLOAD_CACHE_HITS = 0
_ENDPOINT_PAYLOAD_CACHE_MISSES = 0
_PRE_EARNINGS_CACHE_REVISION_LOCK = threading.Lock()
_PRE_EARNINGS_SOURCE_REVISION: Optional[tuple] = None
_PRE_EARNINGS_WARMUP_REVISION: Optional[tuple] = None


def _endpoint_payload_cache_evict():
    """Remove oldest entries when cache exceeds max size."""
    if len(_ENDPOINT_PAYLOAD_CACHE) > _ENDPOINT_PAYLOAD_CACHE_MAX:
        keys_to_remove = list(_ENDPOINT_PAYLOAD_CACHE.keys())[:len(_ENDPOINT_PAYLOAD_CACHE) - _ENDPOINT_PAYLOAD_CACHE_MAX]
        for k in keys_to_remove:
            _ENDPOINT_PAYLOAD_CACHE.pop(k, None)

_CONNECTIVITY_LOCK = threading.Lock()
_CONNECTIVITY_CACHE: Dict[str, Any] = {"ts": 0.0, "data": {}}


def _news_tab_state_ttl_seconds(config_data: Optional[dict]) -> float:
    try:
        ttl_raw = ((config_data or {}).get("news") or {}).get("state_endpoint_ttl_seconds", 10)
        ttl = float(ttl_raw)
    except Exception:
        ttl = 10.0
    # Keep TTL short to avoid stale UI while still collapsing request bursts.
    return max(5.0, min(ttl, 30.0))


def _ordered_watchlist_tickers(config_obj, apply_limit: bool = True) -> List[str]:
    watchlist = config_obj.watchlist or {}
    stocks = list(watchlist.get("yahoo", []) or watchlist.get("stocks", []) or [])
    crypto = list(watchlist.get("binance", []) or watchlist.get("crypto", []) or watchlist.get("cryptos", []) or [])
    ordered = list(watchlist.get("order", []) or [])

    known = set(str(t or "").upper().strip() for t in (stocks + crypto) if str(t or "").strip())
    out: List[str] = []
    seen = set()
    for raw in ordered + stocks + crypto:
        tk = str(raw or "").upper().strip()
        if not tk or tk not in known or tk in seen:
            continue
        seen.add(tk)
        out.append(tk)

    try:
        limit_tickers = int((((config_obj.config or {}).get("news") or {}).get("state_limit_tickers", 50) or 50))
    except Exception:
        limit_tickers = 50
    if apply_limit and limit_tickers > 0:
        out = out[:limit_tickers]
    return out


def _clear_news_tab_state_cache() -> None:
    with _NEWS_TAB_STATE_CACHE_LOCK:
        _NEWS_TAB_STATE_CACHE["key"] = None
        _NEWS_TAB_STATE_CACHE["ts"] = 0.0
        _NEWS_TAB_STATE_CACHE["data"] = None
        _NEWS_TAB_STATE_CACHE["payload"] = None
    with _NEWS_TAB_REQ_CACHE_LOCK:
        _NEWS_TAB_REQ_CACHE["ts"] = 0.0
        _NEWS_TAB_REQ_CACHE["active_tickers"] = []
        _NEWS_TAB_REQ_CACHE["news_enabled"] = True
        _NEWS_TAB_REQ_CACHE["ttl_seconds"] = 10.0
        _NEWS_TAB_REQ_CACHE["config"] = {}


def _get_news_tab_state_stale_payload(cache_key) -> Optional[bytes]:
    with _NEWS_TAB_STATE_CACHE_LOCK:
        cached_key = _NEWS_TAB_STATE_CACHE.get("key")
        cached_payload = _NEWS_TAB_STATE_CACHE.get("payload")
        if cached_key == cache_key and cached_payload is not None:
            return cached_payload
    return None

def _normalize_active_tickers(raw_items: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in raw_items or []:
        tk = str(raw or "").upper().strip()
        if not tk or tk in seen:
            continue
        seen.add(tk)
        out.append(tk)
    return out


def _news_tab_request_snapshot(max_age_seconds: float = 30.0, active_tickers_override: Optional[List[str]] = None):
    override = _normalize_active_tickers(active_tickers_override) if active_tickers_override is not None else None
    if override is not None:
        from core.config import ConfigManager

        config = ConfigManager()
        config_data = dict(config.config or {})
        news_enabled = _agent_enabled("news_tab", config_data)
        ttl_seconds = _news_tab_state_ttl_seconds(config_data)
        return override, news_enabled, ttl_seconds, config_data

    now_ts = time.time()
    with _NEWS_TAB_REQ_CACHE_LOCK:
        cached_ts = float(_NEWS_TAB_REQ_CACHE.get("ts") or 0.0)
        if (now_ts - cached_ts) <= max_age_seconds and _NEWS_TAB_REQ_CACHE.get("config"):
            return (
                list(_NEWS_TAB_REQ_CACHE.get("active_tickers") or []),
                bool(_NEWS_TAB_REQ_CACHE.get("news_enabled")),
                float(_NEWS_TAB_REQ_CACHE.get("ttl_seconds") or 10.0),
                dict(_NEWS_TAB_REQ_CACHE.get("config") or {}),
            )

    from core.config import ConfigManager

    config = ConfigManager()
    # Pass full watchlist context to NewsTabAgent; payload capping is handled inside agent.
    # This avoids starving newly added tail tickers when watchlist is larger than state_limit_tickers.
    active_tickers = _ordered_watchlist_tickers(config, apply_limit=False)
    config_data = dict(config.config or {})
    news_enabled = _agent_enabled("news_tab", config_data)
    ttl_seconds = _news_tab_state_ttl_seconds(config_data)

    with _NEWS_TAB_REQ_CACHE_LOCK:
        _NEWS_TAB_REQ_CACHE["ts"] = time.time()
        _NEWS_TAB_REQ_CACHE["active_tickers"] = list(active_tickers)
        _NEWS_TAB_REQ_CACHE["news_enabled"] = bool(news_enabled)
        _NEWS_TAB_REQ_CACHE["ttl_seconds"] = float(ttl_seconds)
        _NEWS_TAB_REQ_CACHE["config"] = dict(config_data)

    return active_tickers, news_enabled, ttl_seconds, config_data


def _calendar_events_cached(days: int, ttl_seconds: float = 60.0):
    now_ts = time.time()
    key = int(days)
    with _CALENDAR_EVENTS_CACHE_LOCK:
        cached_key = _CALENDAR_EVENTS_CACHE.get("key")
        cached_ts = float(_CALENDAR_EVENTS_CACHE.get("ts") or 0.0)
        payload = _CALENDAR_EVENTS_CACHE.get("payload")
        if payload is None or cached_key != key or (now_ts - cached_ts) > ttl_seconds:
            return None
        return payload


def _set_calendar_events_cache(days: int, payload: bytes):
    with _CALENDAR_EVENTS_CACHE_LOCK:
        _CALENDAR_EVENTS_CACHE["key"] = int(days)
        _CALENDAR_EVENTS_CACHE["ts"] = time.time()
        _CALENDAR_EVENTS_CACHE["payload"] = payload


def _endpoint_payload_get(cache_key: str, ttl_seconds: float):
    global _ENDPOINT_PAYLOAD_CACHE_HITS, _ENDPOINT_PAYLOAD_CACHE_MISSES
    now_ts = time.time()
    with _ENDPOINT_PAYLOAD_CACHE_LOCK:
        entry = _ENDPOINT_PAYLOAD_CACHE.get(cache_key) or {}
        payload = entry.get("payload")
        ts = float(entry.get("ts") or 0.0)
        if entry:
            entry["ttl_seconds"] = float(ttl_seconds)
        if payload is None or (now_ts - ts) > ttl_seconds:
            _ENDPOINT_PAYLOAD_CACHE_MISSES += 1
            return None
        _ENDPOINT_PAYLOAD_CACHE_HITS += 1
        _set_ctx_cache_reason("HIT")
        return payload


def _endpoint_payload_set(cache_key: str, payload: bytes):
    with _ENDPOINT_PAYLOAD_CACHE_LOCK:
        previous = _ENDPOINT_PAYLOAD_CACHE.get(cache_key) or {}
        _ENDPOINT_PAYLOAD_CACHE[cache_key] = {
            "ts": time.time(),
            "payload": payload,
            "ttl_seconds": previous.get("ttl_seconds"),
        }
        _endpoint_payload_cache_evict()


def _endpoint_payload_clear(cache_key: str):
    with _ENDPOINT_PAYLOAD_CACHE_LOCK:
        _ENDPOINT_PAYLOAD_CACHE.pop(cache_key, None)


def _endpoint_payload_clear_prefix(prefix: str):
    pref = str(prefix or "")
    if not pref:
        return
    with _ENDPOINT_PAYLOAD_CACHE_LOCK:
        for key in list(_ENDPOINT_PAYLOAD_CACHE.keys()):
            if str(key).startswith(pref):
                _ENDPOINT_PAYLOAD_CACHE.pop(key, None)


# Bumped whenever hot-list membership changes. A /api/state or
# /api/hotlist/state build that started BEFORE the change must not store its
# (pre-change) result after the caches were cleared: that put the old hot list
# back for a full TTL (up to 45 s on /api/state).
_HOTLIST_MEMBERSHIP_EPOCH = {"value": 0}
_HOTLIST_MEMBERSHIP_EPOCH_LOCK = threading.Lock()


def _hotlist_membership_epoch() -> int:
    with _HOTLIST_MEMBERSHIP_EPOCH_LOCK:
        return int(_HOTLIST_MEMBERSHIP_EPOCH["value"])


def _invalidate_capital_state_cache() -> None:
    """A manual Capital write must be on the next /api/state poll.

    /api/state answers from a snapshot for up to 45 s and then once more while
    it rebuilds. After a manual open / close / stop-target edit / reset the UI
    re-read that snapshot and put the account back to its pre-trade state:
    a position just opened vanished for ~10-20 s, a closed one came back.
    The epoch bump keeps a rebuild that started before the write from storing
    its (pre-write) result.
    """
    with _HOTLIST_MEMBERSHIP_EPOCH_LOCK:
        _HOTLIST_MEMBERSHIP_EPOCH["value"] += 1
    _endpoint_payload_clear_prefix("api_state:")


def _invalidate_hotlist_caches() -> None:
    """Drop every cached response that embeds hot-list membership."""
    with _HOTLIST_MEMBERSHIP_EPOCH_LOCK:
        _HOTLIST_MEMBERSHIP_EPOCH["value"] += 1
    _endpoint_payload_clear("watchlist_state")
    _endpoint_payload_clear("hotlist_state")
    _endpoint_payload_clear_prefix("api_state:")
    # The single-flight group keeps a finished build for 5 s and lets new
    # callers join a running one: forget both, so the next /api/hotlist/state
    # request builds from the new membership.
    try:
        hotlist_flight.invalidate("hotlist_state")
    except Exception as exc:
        logger.debug("Hot list single-flight not invalidated: %s", exc)
    # /api/hotlist/heat keeps its own stale-while-revalidate snapshot (the
    # "N in your hot list" strip). Age it out and start its refresh now, so
    # the tab's next poll gets the new curated list instead of the old one.
    try:
        from api import hotlist_service as _hotlist_service

        heat_cache = getattr(_hotlist_service, "_heat_cache", None)
        if isinstance(heat_cache, dict) and heat_cache.get("payload") is not None:
            heat_cache["at"] = 0.0
            if not IS_TEST_MODE:  # no background provider work from unit tests
                threading.Thread(
                    target=_hotlist_service.build_heat_list, name="heat-invalidate", daemon=True
                ).start()
    except Exception as exc:
        logger.debug("Hot list heat cache not invalidated: %s", exc)


def _pre_earnings_source_revision() -> Optional[tuple]:
    """Return a cheap revision for the agent's atomically persisted snapshot."""
    try:
        from agents.pre_earnings_agent import PRE_EARNINGS_OUTPUT_PATH

        path = Path(PRE_EARNINGS_OUTPUT_PATH).resolve()
    except Exception:
        return None
    try:
        stat = path.stat()
        return (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        # File creation is itself a revision and must invalidate an empty or
        # restart-era endpoint cache.
        return (str(path), None, None)


def _prefer_newer_pre_earnings_snapshot(memory_state: Dict[str, Any]) -> Dict[str, Any]:
    """Prefer the atomically persisted state when another process is newer.

    The API and engine can each hold a PreEarningsAgent singleton.  A local
    singleton being ready does not make its snapshot authoritative when the
    peer has already committed a higher state revision to the shared file.
    """
    current = memory_state if isinstance(memory_state, dict) else {}
    current_revision = int(current.get("state_revision") or 0)
    if current_revision <= 0:
        return current
    try:
        from agents.pre_earnings_agent import PRE_EARNINGS_OUTPUT_PATH

        persisted = _load_json_file_payload(PRE_EARNINGS_OUTPUT_PATH)
    except Exception:
        return current
    if not isinstance(persisted, dict):
        return current
    try:
        persisted_revision = int(persisted.get("state_revision") or 0)
    except (TypeError, ValueError):
        return current
    return persisted if persisted_revision > current_revision else current


def _pre_earnings_warmup_revision(status: Optional[Dict[str, Any]]) -> tuple:
    warm = dict(status or {})
    return (
        str(warm.get("job_id") or ""),
        bool(warm.get("running")),
        int(warm.get("processed") or 0),
        int(warm.get("success") or 0),
        int(warm.get("failed") or 0),
        str(warm.get("last_progress_at") or ""),
        str(warm.get("last_finished") or ""),
        tuple(str(symbol or "").upper().strip() for symbol in (warm.get("queued_symbols") or [])),
    )


def _sync_pre_earnings_cache_revision(
    *,
    warmup_status: Optional[Dict[str, Any]] = None,
) -> bool:
    """Bust the 60s GET cache when persisted or warmup state advances."""
    global _PRE_EARNINGS_SOURCE_REVISION, _PRE_EARNINGS_WARMUP_REVISION

    source_revision = _pre_earnings_source_revision()
    warmup_revision = (
        _pre_earnings_warmup_revision(warmup_status)
        if warmup_status is not None
        else None
    )
    changed = False
    with _PRE_EARNINGS_CACHE_REVISION_LOCK:
        if source_revision is not None:
            if (
                _PRE_EARNINGS_SOURCE_REVISION is not None
                and source_revision != _PRE_EARNINGS_SOURCE_REVISION
            ):
                changed = True
            _PRE_EARNINGS_SOURCE_REVISION = source_revision
        if warmup_revision is not None:
            if (
                _PRE_EARNINGS_WARMUP_REVISION is not None
                and warmup_revision != _PRE_EARNINGS_WARMUP_REVISION
            ):
                changed = True
            _PRE_EARNINGS_WARMUP_REVISION = warmup_revision
    if changed:
        _endpoint_payload_clear("pre_earnings_state")
    return changed


def _invalidate_pre_earnings_state_cache(
    *,
    warmup_status: Optional[Dict[str, Any]] = None,
) -> None:
    """Clear the GET cache and establish the pre-mutation source baseline."""
    global _PRE_EARNINGS_SOURCE_REVISION, _PRE_EARNINGS_WARMUP_REVISION

    _endpoint_payload_clear("pre_earnings_state")
    source_revision = _pre_earnings_source_revision()
    warmup_revision = (
        _pre_earnings_warmup_revision(warmup_status)
        if warmup_status is not None
        else None
    )
    with _PRE_EARNINGS_CACHE_REVISION_LOCK:
        _PRE_EARNINGS_SOURCE_REVISION = source_revision
        # Subsequent warmup progress/finalization invalidates any intermediate
        # GET snapshot. When no job status exists, the next live status becomes
        # the baseline.
        _PRE_EARNINGS_WARMUP_REVISION = warmup_revision


def _live_pre_earnings_warmup_status(agent: Any) -> Optional[Dict[str, Any]]:
    if not hasattr(agent, "get_coverage_warmup_status"):
        return None
    try:
        status = agent.get_coverage_warmup_status()
        return dict(status or {}) if isinstance(status, dict) else None
    except Exception:
        return None


def _pre_earnings_coverage_symbol_limit() -> int:
    """Read the configurable explicit-symbol cap, bounded by a hard ceiling."""
    raw_limit: Any = os.getenv("PRE_EARNINGS_COVERAGE_WARMUP_MAX_SYMBOLS")
    if raw_limit in (None, ""):
        try:
            config_data = dict(ConfigManager().config or {})
            raw_limit = (
                (config_data.get("pre_earnings") or {}).get(
                    "coverage_warmup_max_symbols",
                    _PRE_EARNINGS_COVERAGE_SYMBOLS_DEFAULT_LIMIT,
                )
            )
        except Exception:
            raw_limit = _PRE_EARNINGS_COVERAGE_SYMBOLS_DEFAULT_LIMIT
    try:
        parsed = int(raw_limit)
    except (TypeError, ValueError):
        parsed = _PRE_EARNINGS_COVERAGE_SYMBOLS_DEFAULT_LIMIT
    return max(1, min(parsed, _PRE_EARNINGS_COVERAGE_SYMBOLS_HARD_LIMIT))


def _normalize_pre_earnings_coverage_symbols(
    raw_symbols: Optional[List[str]],
) -> List[str]:
    normalized: List[str] = []
    seen = set()
    invalid: List[str] = []
    for raw_symbol in raw_symbols or []:
        symbol = str(raw_symbol or "").upper().strip()
        if not symbol or _PRE_EARNINGS_SYMBOL_RE.fullmatch(symbol) is None:
            invalid.append(str(raw_symbol or "")[:32])
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)

    if invalid:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Invalid pre-earnings symbol list.",
                "invalid_symbols": invalid[:10],
            },
        )

    limit = _pre_earnings_coverage_symbol_limit()
    if len(normalized) > limit:
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"At most {limit} unique symbols may be warmed per request.",
                "max_symbols": limit,
                "requested_count": len(normalized),
            },
        )
    return normalized


def _endpoint_payload_peek(cache_key: str):
    with _ENDPOINT_PAYLOAD_CACHE_LOCK:
        entry = _ENDPOINT_PAYLOAD_CACHE.get(cache_key) or {}
        return entry.get("payload")


def _runtime_cache_inventory() -> Dict[str, Any]:
    """Summarize bespoke runtime caches without exposing cached values."""
    now_ts = time.time()
    with _ENDPOINT_PAYLOAD_CACHE_LOCK:
        endpoint_entries = []
        for key, entry in _ENDPOINT_PAYLOAD_CACHE.items():
            age = max(0.0, now_ts - float(entry.get("ts") or 0.0))
            ttl = entry.get("ttl_seconds")
            payload = entry.get("payload")
            endpoint_entries.append({
                "key": str(key),
                "age_seconds": round(age, 3),
                "ttl_seconds": round(float(ttl), 3) if ttl is not None else None,
                "is_expired": bool(ttl is not None and age > float(ttl)),
                "payload_bytes": len(payload) if isinstance(payload, (bytes, bytearray)) else None,
            })
        endpoint_layer = {
            "size": len(endpoint_entries),
            "max_size": int(_ENDPOINT_PAYLOAD_CACHE_MAX),
            "hit_total": int(_ENDPOINT_PAYLOAD_CACHE_HITS),
            "miss_total": int(_ENDPOINT_PAYLOAD_CACHE_MISSES),
            "entries": sorted(endpoint_entries, key=lambda row: row["key"]),
        }

    with _chart_cache_lock:
        chart_entries = []
        for key, entry in _chart_cache.items():
            timeframe = str(key).rsplit(":", 1)[-1]
            ttl = float(_CHART_TTL_BY_TIMEFRAME.get(timeframe, 0.0))
            age = max(0.0, now_ts - float(entry.get("created_at") or 0.0))
            chart_entries.append({
                "key": str(key),
                "age_seconds": round(age, 3),
                "ttl_seconds": ttl,
                "is_expired": bool(ttl > 0 and age > ttl),
            })
        chart_layer = {"size": len(chart_entries), "entries": chart_entries}

    with _sparkline_lock:
        sparkline_ages = [
            max(0.0, now_ts - float((entry or {}).get("ts") or 0.0))
            for entry in _sparkline_cache.values()
        ]
        sparkline_layer = {
            "size": len(_sparkline_cache),
            "ttl_seconds": float(_SPARKLINE_TTL),
            "oldest_age_seconds": round(max(sparkline_ages), 3) if sparkline_ages else None,
        }

    with _NEWS_TAB_STATE_CACHE_LOCK:
        news_ts = float(_NEWS_TAB_STATE_CACHE.get("ts") or 0.0)
        news_layer = {
            "size": 1 if _NEWS_TAB_STATE_CACHE.get("payload") is not None else 0,
            "age_seconds": round(max(0.0, now_ts - news_ts), 3) if news_ts else None,
        }
    with _CALENDAR_EVENTS_CACHE_LOCK:
        calendar_ts = float(_CALENDAR_EVENTS_CACHE.get("ts") or 0.0)
        calendar_layer = {
            "size": 1 if _CALENDAR_EVENTS_CACHE.get("payload") is not None else 0,
            "age_seconds": round(max(0.0, now_ts - calendar_ts), 3) if calendar_ts else None,
        }
    with _CONNECTIVITY_LOCK:
        connectivity_ts = float(_CONNECTIVITY_CACHE.get("ts") or 0.0)
        connectivity_layer = {
            "size": len(_CONNECTIVITY_CACHE.get("data") or {}),
            "age_seconds": round(max(0.0, now_ts - connectivity_ts), 3) if connectivity_ts else None,
        }

    layers = {
        "endpoint_response": endpoint_layer,
        "chart": chart_layer,
        "sparkline": sparkline_layer,
        "news_tab_state": news_layer,
        "calendar_events": calendar_layer,
        "connectivity": connectivity_layer,
    }
    return {
        "layers": layers,
        "total_entries": sum(int(layer.get("size", 0)) for layer in layers.values()),
    }


def _parse_include_flags(include: Optional[str]) -> Optional[set[str]]:
    raw = str(include or "").strip()
    if not raw:
        return None
    parts = [p.strip().lower() for p in raw.split(",")]
    return {p for p in parts if p}


def _stable_payload_version(payload: Dict[str, Any], drop_keys: Optional[set[str]] = None) -> str:
    if not isinstance(payload, dict):
        return "na"
    redacted = dict(payload)
    for key in (drop_keys or set()):
        redacted.pop(key, None)
    try:
        blob = json.dumps(redacted, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except Exception:
        blob = str(redacted)
    digest = hashlib.sha1(blob.encode("utf-8")).hexdigest()
    return digest[:16]


def _cockpit_stable_version(payload: Dict[str, Any]) -> str:
    """Content version for /api/cockpit unchanged-polling.

    Volatile per-rebuild fields are removed or quantized so the version only
    changes when OWNER-VISIBLE content changes: health `checked_at` stamps and
    `header.market.now_et` are pure render moments, and `*_age_seconds`
    freshness counters are bucketed to whole minutes (the UI still receives a
    fresh payload at most a minute later, while identical-content rebuilds
    return `unchanged` instead of re-transferring the whole document)."""
    if not isinstance(payload, dict):
        return "na"

    volatile_exact = {"generated_at", "now_et", "checked_at"}

    def normalize(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: (int(value // 60) if isinstance(value, (int, float)) and not isinstance(value, bool)
                      and key.endswith("age_seconds") and value >= 0 else normalize(value))
                for key, value in node.items() if key not in volatile_exact
            }
        if isinstance(node, list):
            return [normalize(item) for item in node]
        return node

    return _stable_payload_version(normalize(payload))


def _filter_state_payload_include(payload: Dict[str, Any], include_flags: Optional[set[str]]) -> Dict[str, Any]:
    if not include_flags:
        return payload
    keep = {
        "state_version",
        "timestamp",
        "refresh_interval_ms",
        "unchanged",
        "version",
        "api_stats",
    }
    include_map = {
        "signals": {"signals", "signals_raw"},
        "stats": {"stats", "backtest_summary"},
        "news": {"news", "breaking_news", "market_news", "ticker_news"},
        "hotlist": {"hot_list"},
        "watchlist": {"watchlist", "watchlist_events"},
        "capital": {"capital"},
        "log": {"log", "activity_log"},
        "tickers": {"tickers"},
        "scanner": {"scanner"},
    }
    for flag in include_flags:
        keep.update(include_map.get(flag, set()))
    return {k: v for k, v in payload.items() if k in keep}


def _filter_dashboard_payload_include(payload: Dict[str, Any], include_flags: Optional[set[str]]) -> Dict[str, Any]:
    if not include_flags:
        return payload
    keep = {
        "dashboard_version",
        "dashboard_state_version",
        "dashboard_generated_at",
        "unchanged",
        "version",
    }
    include_map = {
        "news": {"dashboard_news_items", "dashboard_market_narrative"},
        "earnings": {"dashboard_earnings_7d"},
        "health": {"dashboard_risk_flags", "dashboard_strategy_health", "dashboard_confidence_index"},
        "regime": {"dashboard_regime"},
        "watchlist": {"dashboard_watchlist_priority"},
    }
    for flag in include_flags:
        keep.update(include_map.get(flag, set()))
    return {k: v for k, v in payload.items() if k in keep}


def _reset_quality_gate_endpoint_caches() -> None:
    global _ENDPOINT_PAYLOAD_CACHE_HITS, _ENDPOINT_PAYLOAD_CACHE_MISSES
    global _PRE_EARNINGS_SOURCE_REVISION, _PRE_EARNINGS_WARMUP_REVISION
    with _CALENDAR_EVENTS_CACHE_LOCK:
        _CALENDAR_EVENTS_CACHE["key"] = None
        _CALENDAR_EVENTS_CACHE["ts"] = 0.0
        _CALENDAR_EVENTS_CACHE["payload"] = None
    with _ENDPOINT_PAYLOAD_CACHE_LOCK:
        _ENDPOINT_PAYLOAD_CACHE.clear()
        _ENDPOINT_PAYLOAD_CACHE_HITS = 0
        _ENDPOINT_PAYLOAD_CACHE_MISSES = 0
    with _PRE_EARNINGS_CACHE_REVISION_LOCK:
        _PRE_EARNINGS_SOURCE_REVISION = None
        _PRE_EARNINGS_WARMUP_REVISION = None
    _clear_news_tab_state_cache()


def _tcp_connectivity_check(host: str, port: int = 443, timeout_seconds: float = 1.5) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, int(port)), timeout=float(timeout_seconds)):
            latency_ms = (time.perf_counter() - started) * 1000.0
            return {
                "host": host,
                "port": int(port),
                "ok": True,
                "latency_ms": round(latency_ms, 2),
                "error": None,
            }
    except Exception as e:
        latency_ms = (time.perf_counter() - started) * 1000.0
        return {
            "host": host,
            "port": int(port),
            "ok": False,
            "latency_ms": round(latency_ms, 2),
            "error": str(e),
        }


def _external_connectivity_snapshot() -> Dict[str, Any]:
    if str(os.getenv("MS_DISABLE_CONNECTIVITY_CHECK", "0")).strip().lower() in {"1", "true", "yes", "on"}:
        return {"enabled": False, "providers": {}}

    try:
        ttl_seconds = float(os.getenv("MS_DIAGNOSTICS_CONNECTIVITY_TTL_SECONDS", "30"))
    except Exception:
        ttl_seconds = 30.0
    ttl_seconds = max(5.0, min(ttl_seconds, 300.0))
    now_ts = time.time()

    with _CONNECTIVITY_LOCK:
        cached_ts = float(_CONNECTIVITY_CACHE.get("ts") or 0.0)
        cached_data = _CONNECTIVITY_CACHE.get("data")
        if isinstance(cached_data, dict) and (now_ts - cached_ts) <= ttl_seconds:
            return dict(cached_data)

    providers = {
        "yahoo": _tcp_connectivity_check("query1.finance.yahoo.com", 443, timeout_seconds=1.5),
    }
    connected = sum(1 for item in providers.values() if isinstance(item, dict) and item.get("ok"))
    payload = {
        "enabled": True,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ttl_seconds": int(ttl_seconds),
        "providers": providers,
        "connected_providers": int(connected),
        "total_providers": len(providers),
    }
    with _CONNECTIVITY_LOCK:
        _CONNECTIVITY_CACHE["ts"] = now_ts
        _CONNECTIVITY_CACHE["data"] = dict(payload)
    return payload


def _build_news_tab_state_cached(cache_key, ttl_seconds: float, builder):
    now_ts = time.time()
    leader = False
    wait_event = None
    cache_label = f"news-tab-state:{repr(cache_key)}"

    # Critical section holds ONLY the cache-dict lookup. Observability
    # (record_manual_decision -> record_cache_decision) builds a payload, calls
    # _utc_now_iso(), and acquires a SECOND lock (observability _LOCK). Doing
    # that inside _NEWS_TAB_STATE_CACHE_LOCK serialized every concurrent HIT on
    # one global lock (50-concurrent burst p95 ~528ms). Record OUTSIDE the lock
    # so the critical section is a dict lookup; concurrent HITs no longer block
    # on each other's observability work.
    with _NEWS_TAB_STATE_CACHE_LOCK:
        cached_key = _NEWS_TAB_STATE_CACHE.get("key")
        cached_ts = float(_NEWS_TAB_STATE_CACHE.get("ts") or 0.0)
        cached_data = _NEWS_TAB_STATE_CACHE.get("data")
        cached_payload = _NEWS_TAB_STATE_CACHE.get("payload")
        if (
            ttl_seconds > 0
            and cached_key == cache_key
            and cached_data is not None
            and cached_payload is not None
            and (now_ts - cached_ts) <= ttl_seconds
        ):
            hit_payload = cached_payload
            hit_age = now_ts - cached_ts
        else:
            hit_payload = None
            hit_age = None
            inflight = _NEWS_TAB_STATE_INFLIGHT.get(cache_key)
            if inflight is None:
                wait_event = threading.Event()
                _NEWS_TAB_STATE_INFLIGHT[cache_key] = {"event": wait_event}
                leader = True
            else:
                wait_event = inflight.get("event")

    if hit_payload is not None:
        _set_ctx_cache_reason("HIT")
        cache_manager.record_manual_decision(
            key=cache_label,
            decision="HIT",
            reason="state endpoint cache hit",
            source="news_tab_state",
            ttl_seconds=int(ttl_seconds),
            age_seconds=hit_age,
        )
        return hit_payload

    if leader:
        try:
            built = builder() or {}
            built_payload = json.dumps(built, ensure_ascii=False, default=str).encode("utf-8")
        except Exception:
            with _NEWS_TAB_STATE_CACHE_LOCK:
                entry = _NEWS_TAB_STATE_INFLIGHT.pop(cache_key, None)
                if entry and entry.get("event"):
                    entry["event"].set()
            raise

        with _NEWS_TAB_STATE_CACHE_LOCK:
            if ttl_seconds > 0:
                _NEWS_TAB_STATE_CACHE["key"] = cache_key
                _NEWS_TAB_STATE_CACHE["ts"] = time.time()
                _NEWS_TAB_STATE_CACHE["data"] = built
                _NEWS_TAB_STATE_CACHE["payload"] = built_payload
            entry = _NEWS_TAB_STATE_INFLIGHT.pop(cache_key, None)
            if entry and entry.get("event"):
                entry["event"].set()
        cache_manager.record_manual_decision(
            key=cache_label,
            decision="MISS",
            reason="state endpoint rebuilt",
            source="news_tab_state",
            ttl_seconds=int(ttl_seconds),
            age_seconds=None,
        )
        _set_ctx_cache_reason("MISS")
        return built_payload

    if wait_event is not None:
        wait_event.wait(timeout=35.0)

    # After-wait cache re-check: same treatment — record OUTSIDE the lock.
    with _NEWS_TAB_STATE_CACHE_LOCK:
        cached_key = _NEWS_TAB_STATE_CACHE.get("key")
        cached_ts = float(_NEWS_TAB_STATE_CACHE.get("ts") or 0.0)
        cached_data = _NEWS_TAB_STATE_CACHE.get("data")
        cached_payload = _NEWS_TAB_STATE_CACHE.get("payload")
        if (
            ttl_seconds > 0
            and cached_key == cache_key
            and cached_data is not None
            and cached_payload is not None
            and (time.time() - cached_ts) <= ttl_seconds
        ):
            after_wait_payload = cached_payload
            after_wait_age = time.time() - cached_ts
        else:
            after_wait_payload = None
            after_wait_age = None

    if after_wait_payload is not None:
        _set_ctx_cache_reason("HIT")
        cache_manager.record_manual_decision(
            key=cache_label,
            decision="HIT",
            reason="state endpoint cache hit after wait",
            source="news_tab_state",
            ttl_seconds=int(ttl_seconds),
            age_seconds=after_wait_age,
        )
        return after_wait_payload

    cache_manager.record_manual_decision(
        key=cache_label,
        decision="MISS",
        reason="state endpoint fallback build",
        source="news_tab_state",
        ttl_seconds=int(ttl_seconds),
        age_seconds=None,
    )
    _set_ctx_cache_reason("MISS")
    built = builder() or {}
    return json.dumps(built, ensure_ascii=False, default=str).encode("utf-8")


@app.get("/api/news-tab/state")
def api_news_tab_state(active_tickers: Optional[str] = None):
    """Get enhanced news tab state"""
    try:
        from agents.news_tab_agent import (
            get_news_tab_agent_if_ready,
            get_news_tab_agent_status,
            kickoff_news_tab_agent_init,
            _news_tab_agent_ready,
        )

        override = None
        if active_tickers is not None:
            override = [part for part in str(active_tickers).split(",")]
        active_tickers, news_enabled, ttl_seconds, config_data = _news_tab_request_snapshot(
            active_tickers_override=override
        )

        if os.getenv("MS_DISABLE_NEWS_AGENT_INIT", "0") == "1":
            return {
                "ticker_news": {},
                "market_news": [],
                "breaking_news": [],
                "last_update": None,
                "active_filters": [],
                "total_articles": 0,
                "loading": False,
                "disabled": False,
                "initializing": False,
                "error": "News agent initialization disabled for isolated test server.",
            }

        if not news_enabled:
            agent = get_news_tab_agent_if_ready()
            if agent is not None:
                cache_key = ("news_tab_state", "disabled", tuple(active_tickers))
                payload = _build_news_tab_state_cached(
                    cache_key,
                    ttl_seconds,
                    lambda: {
                        **(agent.get_news_tab_state(active_tickers=active_tickers) or {}),
                        "loading": False,
                        "disabled": True,
                    },
                )
                return Response(content=payload, media_type="application/json")
            return {
                "ticker_news": {},
                "market_news": [],
                "breaking_news": [],
                "last_update": None,
                "active_filters": [],
                "total_articles": 0,
                "loading": False,
                "disabled": True,
                "initializing": False,
                "error": None,
            }

        # Non-blocking endpoint: if news cache is still hydrating, wait briefly
        # before returning loading payload. This avoids a race condition where
        # the agent is initializing but not yet ready.
        kickoff_news_tab_agent_init(config_data)
        agent = get_news_tab_agent_if_ready()
        if agent is None and _news_tab_agent_ready is not None:
            # Wait up to 8 seconds for the agent to finish initializing.
            # The backup load can take a few seconds for a large file.
            _news_tab_agent_ready.wait(timeout=8.0)
            agent = get_news_tab_agent_if_ready()
        if IS_TEST_MODE and agent is None:
            try:
                from agents.news_tab_agent import get_news_tab_agent
                agent = get_news_tab_agent(config_data)
            except Exception as init_err:
                logger.warning(f"NewsTabAgent sync init failed in TEST_MODE: {init_err}")
                agent = None
        if agent is None:
            status = get_news_tab_agent_status()
            return {
                "ticker_news": {},
                "market_news": [],
                "breaking_news": [],
                "last_update": None,
                "active_filters": [],
                "total_articles": 0,
                "loading": True,
                "disabled": False,
                "initializing": bool(status.get("initializing")),
                "error": status.get("error"),
            }

        cache_key = ("news_tab_state", "enabled", tuple(active_tickers))
        build_payload = lambda: {
            **(agent.get_news_tab_state(active_tickers=active_tickers) or {}),
            "loading": False,
            "disabled": False,
        }
        # ``cache_only`` forbids provider I/O; it must not mean "serve the first
        # snapshot forever". This builder reads only the agent's in-memory
        # state, so the normal TTL-aware cache path is safe and lets completed
        # background refreshes become visible to the UI within 5-30 seconds.
        payload = _build_news_tab_state_cached(
            cache_key,
            ttl_seconds,
            build_payload,
        )
        return Response(content=payload, media_type="application/json")
        
    except Exception as e:
        logger.error(f"Error getting news tab state: {e}", exc_info=True)
        raise _safe_error()

# Background Worker Lifecycle (impl functions used by lifespan)
async def startup_worker_impl():
    try:
        if os.getenv("MS_DISABLE_BG_WORKER", "0") == "1":
            logger.info("Background worker disabled by MS_DISABLE_BG_WORKER=1")
            return
        from core.background_worker import worker
        worker.start()
    except Exception as e:
        logger.error(f"Failed to start analysis worker: {e}")

async def shutdown_worker_impl():
    try:
        from core.background_worker import worker
        worker.stop()
    except Exception:
        pass



@app.post("/api/news-tab/refresh")
def api_news_tab_refresh():
    """Refresh news for all tickers"""
    try:
        from agents.news_tab_agent import get_news_tab_agent, get_news_tab_agent_if_ready, get_news_tab_agent_status
        from core.config import ConfigManager
        import threading

        config = ConfigManager()
        _clear_news_tab_state_cache()
        if not _agent_enabled("news_tab", config.config):
            return {
                "status": "disabled",
                "tickers": 0,
                "ready": False,
                "initializing": False,
                "error": None,
                "disabled": True,
            }

        tickers = _ordered_watchlist_tickers(config, apply_limit=False)

        def _refresh_worker():
            try:
                agent = get_news_tab_agent()
                agent.refresh_all_news(tickers)
            except Exception as e:
                logger.error(f"Background news refresh failed: {e}")

        threading.Thread(target=_refresh_worker, daemon=True, name="NewsRefresh").start()

        status = get_news_tab_agent_status()
        agent_ready = get_news_tab_agent_if_ready() is not None
        return {
            "status": "started",
            "tickers": len(tickers),
            "ready": agent_ready,
            "initializing": bool(status.get("initializing")),
            "error": status.get("error"),
            "disabled": False,
        }

    except Exception as e:
        logger.error(f"Error refreshing news: {e}", exc_info=True)
        raise _safe_error()

@app.get("/api/news-tab/ticker/{ticker}")
def api_news_tab_ticker(ticker: str, max_items: int = 10, refresh: bool = False):
    """Get news for specific ticker"""
    try:
        if not _agent_enabled("news_tab"):
            return {"news": [], "disabled": True}
        from agents.news_tab_agent import get_news_tab_agent
        from core.config import normalize_ticker

        agent = get_news_tab_agent()
        normalized = normalize_ticker(str(ticker or "").upper().strip(), provider="yahoo") or str(ticker or "").upper().strip()

        ctx = get_request_context()
        forced_cache = bool(ctx and ctx.cache_only)
        ensure_result = agent.ensure_ticker_news_cached(
            normalized,
            force_refresh=bool(refresh),
            allow_remote=not forced_cache,
        )
        if ensure_result.get("fetched"):
            _clear_news_tab_state_cache()

        news_rows = agent.get_ticker_news(normalized, max_items)
        cache_reason = "HIT" if news_rows else "MISS"
        if ensure_result.get("status") in {"refreshed"}:
            cache_reason = "MISS"
        elif ensure_result.get("status") in {"miss_throttled", "miss_cache_only"} and not news_rows:
            cache_reason = "MISS"
        if ctx and isinstance(ctx.metadata, dict):
            ctx.metadata["cache_reason"] = cache_reason

        cache_manager.record_manual_decision(
            key=f"news-ticker:{normalized}",
            decision=cache_reason,
            reason=str(ensure_result.get("reason") or "ticker_endpoint"),
            source="news_ticker_endpoint",
            ttl_seconds=300,
            age_seconds=None,
        )
        return {
            "ticker": normalized,
            "news": news_rows,
            "disabled": False,
            "cache_status": cache_reason,
            "fetch_status": ensure_result.get("status"),
            "fetch_reason": ensure_result.get("reason"),
        }
    except Exception as e:
        logger.error(f"Error getting ticker news: {e}", exc_info=True)
        raise _safe_error()



@app.get("/api/news-tab/breaking")
def api_news_tab_breaking(max_age_minutes: int = 180):
    """Get breaking news.

    Window is intentionally wider (180 min) than the frontend's 60-min
    "breaking freshness" display window. Re-ingested RSS items can have their
    published_at re-parsed a minute or two differently each refresh; a tight
    60-min cliff here made articles oscillate in/out exactly at the display
    edge (drop-then-reappear flicker). With a generous backend window the age
    threshold is crossed once, long after the item stops being shown, so the
    feed is stable. Callers may still override.
    """
    try:
        # --- endpoint-level response cache (15s TTL) ---
        cache_key = f"news_breaking:{max_age_minutes}"
        cached = _endpoint_payload_get(cache_key, ttl_seconds=15.0)
        if cached is not None:
            return Response(content=cached, media_type="application/json")

        if not _agent_enabled("news_tab"):
            return {"breaking": [], "disabled": True}
        if os.getenv("MS_DISABLE_NEWS_AGENT_INIT", "0") == "1":
            return {"breaking": [], "disabled": False, "initializing": False}
        from agents.news_tab_agent import get_news_tab_agent_if_ready, kickoff_news_tab_agent_init

        agent = get_news_tab_agent_if_ready()
        if agent is None:
            # Avoid blocking the UI on first poll while the agent hydrates its on-disk cache.
            kickoff_news_tab_agent_init()
            return {"breaking": [], "disabled": False, "initializing": True}

        result = {"breaking": agent.get_breaking_news(max_age_minutes), "disabled": False}

        try:
            payload_bytes = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
            _endpoint_payload_set(cache_key, payload_bytes)
        except Exception:
            pass
        return result
    except Exception as e:
        logger.error(f"Error getting breaking news: {e}", exc_info=True)
        raise _safe_error()







class SuppressionRequest(BaseModel):
    rule_type: str  # keyword|source|ticker_topic|topic
    value: Optional[str] = None
    ticker: Optional[str] = None
    article_id: Optional[str] = None


@app.get("/api/news-tab/suppression")
def api_news_tab_suppression_get():
    try:
        if not _agent_enabled("news_tab"):
            return {"rules": [], "disabled": True}
        from agents.news_tab_agent import get_news_tab_agent
        agent = get_news_tab_agent()
        data = agent.get_suppression_rules()
        if isinstance(data, dict):
            data["disabled"] = False
        return data
    except Exception as e:
        logger.error(f"Error loading suppression rules: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/news-tab/suppression")
def api_news_tab_suppression_add(request: SuppressionRequest):
    try:
        if not _agent_enabled("news_tab"):
            return {"status": "disabled", "suppression": {}, "disabled": True}
        from agents.news_tab_agent import get_news_tab_agent
        agent = get_news_tab_agent()

        rule_type = (request.rule_type or "").strip().lower()
        if rule_type == "topic":
            value = request.value
            if not value and request.article_id:
                value = agent.get_article_topic_hint(request.article_id)
            rules = agent.add_suppression("keyword", value or "", ticker=request.ticker)
        elif rule_type == "source":
            value = request.value
            if not value and request.article_id:
                detail = agent.get_article_details(request.article_id)
                value = (detail or {}).get("source")
            rules = agent.add_suppression("source", value or "", ticker=request.ticker)
        elif rule_type == "ticker_topic":
            value = request.value
            if not value and request.article_id:
                value = agent.get_article_topic_hint(request.article_id)
            rules = agent.add_suppression("ticker_topic", value or "", ticker=request.ticker)
        else:
            rules = agent.add_suppression("keyword", request.value or "", ticker=request.ticker)

        _clear_news_tab_state_cache()
        return {"status": "ok", "suppression": rules, "disabled": False}
    except Exception as e:
        logger.error(f"Error adding suppression rule: {e}", exc_info=True)
        raise _safe_error()

def _require_positive_days(days: Any) -> None:
    """400 for an explicit ``days <= 0`` (None = use the route default)."""
    if days is None:
        return
    try:
        value = int(days)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="days must be a whole number of at least 1")
    if value <= 0:
        raise HTTPException(status_code=400, detail=f"days must be at least 1 (got {value})")


def _attach_news_briefing(agent: Any, article_id: Any, analysis: Any, analysis_type: str, model: str) -> str:
    """Hand a validated briefing to the news agent's in-memory article.

    The briefing was only written to the analysis store, so the next
    /api/news-tab/state poll (served from the agent's memory) no longer had
    it. Returns the name of the agent method used, or "" when the agent has
    none / the article is unknown / the call failed (never raises).
    """
    if not article_id or not isinstance(analysis, dict):
        return ""
    import inspect

    for name in ("attach_briefing", "store_article_briefing"):
        method = getattr(agent, name, None)
        if not callable(method):
            continue
        try:
            extras = {"analysis_type": analysis_type, "model": model}
            try:
                params = inspect.signature(method).parameters
                takes_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
                extras = {k: v for k, v in extras.items() if takes_any or k in params}
            except (TypeError, ValueError):
                extras = {}
            outcome = method(article_id, analysis, **extras)
        except Exception as exc:
            logger.warning("News briefing not attached to in-memory article %s: %s", article_id, exc)
            return ""
        if outcome is False:
            return ""
        try:
            _clear_news_tab_state_cache()
        except Exception:
            pass
        return name
    return ""


class HistoryRequest(BaseModel):
    ticker: Optional[str] = None  # Single ticker (legacy)
    tickers: Optional[List[str]] = None  # Multiple tickers
    days: Optional[int] = 90
    all_watchlist: Optional[bool] = False  # Fetch for all watchlist tickers

@app.post("/api/news-tab/history")
def api_news_history_fetch(request: HistoryRequest):
    """
    On-demand fetch for historical news (up to 90 days).
    
    Modes:
    - Single ticker: {"ticker": "AAPL", "days": 30}
    - Multiple tickers: {"tickers": ["AAPL", "NVDA"], "days": 90}
    - All watchlist: {"all_watchlist": true, "days": 90}
    """
    try:
        _require_positive_days(request.days)
        if not _agent_enabled("news_tab"):
            return {"status": "disabled", "reason": "news_tab_agent_disabled", "disabled": True}
        from agents.news_tab_agent import get_news_tab_agent
        from core.config import ConfigManager
        
        agent = get_news_tab_agent()
        
        # Determine which tickers to process
        if request.all_watchlist:
            config = ConfigManager()
            tickers = _ordered_watchlist_tickers(config, apply_limit=False)
        elif request.tickers:
            tickers = [str(t or "").upper().strip() for t in request.tickers if str(t or "").strip()]
        elif request.ticker:
            # Single ticker mode (backward compatible)
            result = agent.fetch_historical_news(str(request.ticker or "").upper().strip(), request.days)
            _clear_news_tab_state_cache()
            return result
        else:
            raise HTTPException(status_code=400, detail="Must provide ticker, tickers, or all_watchlist=true")
        
        # Batch mode
        result = agent.fetch_historical_news_all(tickers, request.days)
        _clear_news_tab_state_cache()
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching history: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/news-tab/history/start")
def api_news_history_start(request: HistoryRequest):
    """
    Start async historical fetch job (non-blocking).
    """
    try:
        _require_positive_days(request.days)
        if not _agent_enabled("news_tab"):
            return {"status": "disabled", "job_id": None, "tickers": [], "days": request.days or 90, "disabled": True}
        from agents.news_tab_agent import get_news_tab_agent
        from core.config import ConfigManager

        agent = get_news_tab_agent()

        if request.all_watchlist:
            config = ConfigManager()
            tickers = _ordered_watchlist_tickers(config, apply_limit=False)
        elif request.tickers:
            tickers = [str(t or "").upper().strip() for t in request.tickers if str(t or "").strip()]
        elif request.ticker:
            tickers = [str(request.ticker or "").upper().strip()]
        else:
            raise HTTPException(status_code=400, detail="Must provide ticker, tickers, or all_watchlist=true")

        job_id = agent.start_history_job(tickers=tickers, days=request.days or 90)
        _clear_news_tab_state_cache()
        return {"status": "started", "job_id": job_id, "tickers": tickers, "days": request.days or 90}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting history job: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/news-tab/history/progress/{job_id}")
def api_news_history_progress(job_id: str):
    try:
        if not _agent_enabled("news_tab"):
            return {"status": "disabled", "job_id": job_id, "disabled": True}
        from agents.news_tab_agent import get_news_tab_agent
        agent = get_news_tab_agent()
        job = agent.get_history_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error loading history job: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/news-tab/export")
def api_news_export(
    days: int = 7,
    tickers: Optional[str] = None,  # Comma-separated, None = all
    impact: Optional[str] = None,   # low,medium,high,critical - None = all
    include_tldr: bool = False,
    include_action_score: bool = False,
    include_time_horizon: bool = False,
):
    """
    Export news articles as downloadable TXT file with links.
    
    Args:
        days: Number of days back to include (1-365)
        tickers: Comma-separated ticker filter (e.g., "AAPL,MARA,BTC-USD")
        impact: Comma-separated impact levels (e.g., "high,critical")
    
    Returns:
        TXT file download with format: [DATE] [TICKER] Title\nURL
    """
    from fastapi.responses import PlainTextResponse
    from datetime import datetime, timedelta
    from agents.news_tab_agent import get_news_tab_agent

    # days <= 0 used to produce a cutoff in the future (an empty export that
    # looked like "no news"); say what is wrong instead.
    _require_positive_days(days)

    try:
        if not _agent_enabled("news_tab"):
            return PlainTextResponse(
                content="NEWS FLOW DISABLED\nEnable config.agents.news_tab.enabled to export live news.",
                media_type="text/plain; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=news_export_disabled.txt"},
            )

        ticker_filter = {t.strip().upper() for t in tickers.split(",") if t.strip()} if tickers else None
        impact_filter = {i.strip().lower() for i in impact.split(",") if i.strip()} if impact else None
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        def _parse_pub(value: str):
            if not value:
                return None
            try:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                return None

        # Pull full in-memory article set (without UI payload limits).
        agent = get_news_tab_agent()
        raw_articles = agent.get_all_articles_flat()

        filtered_articles = []
        for art in raw_articles:
            pub_date = _parse_pub(art.get("published_at"))
            if pub_date is None or pub_date < cutoff:
                continue

            ticker_label = str(art.get("ticker") or "MARKET").upper().strip() or "MARKET"
            if ticker_filter and ticker_label not in ticker_filter:
                continue

            art_impact = str(art.get("impact") or "low").lower()
            if impact_filter and art_impact not in impact_filter:
                continue

            filtered_articles.append({
                "id": art.get("id", ""),
                "date": pub_date,
                "ticker": ticker_label,
                "title": art.get("title", "No title"),
                "url": art.get("url", ""),
                "impact": art_impact,
                "source": art.get("source", ""),
                "action_score": art.get("action_score", 0),
                "ai_micro_summary": art.get("ai_micro_summary"),
                "ai_deep_analysis": art.get("ai_deep_analysis"),
            })

        # Dedup by stable article identity + ticker assignment.
        # This preserves multi-ticker stories for both symbols.
        dedup = {}
        for art in filtered_articles:
            identity = art.get("id") or art.get("url") or art.get("title") or "untitled"
            key = f"{identity}|{art.get('ticker', 'MARKET')}"
            prev = dedup.get(key)
            if prev is None:
                dedup[key] = art
                continue
            if art["date"] > prev["date"]:
                dedup[key] = art

        articles = sorted(dedup.values(), key=lambda x: x["date"], reverse=True)
        
        # Generate TXT output
        # Article times below are UTC, so the header is UTC too and both say so.
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + " UTC"
        ticker_str = ", ".join(ticker_filter) if ticker_filter else "ALL"
        impact_str = ", ".join(impact_filter) if impact_filter else "ALL"
        
        lines = [
            "=" * 60,
            "MARKET SIGNALS - NEWS EXPORT",
            f"Generated: {now_str}",
            f"Days: {days} | Tickers: {ticker_str} | Impact: {impact_str}",
            "All times are UTC.",
            (
                "Include:"
                f" TLDR={include_tldr},"
                f" ActionScore={include_action_score},"
                f" TimeHorizon={include_time_horizon}"
            ),
            f"Total Articles: {len(articles)}",
            "=" * 60,
            ""
        ]
        
        for art in articles:
            date_str = art["date"].strftime("%Y-%m-%d %H:%M") + " UTC"
            ticker_label = art["ticker"].upper()
            impact_label = art["impact"].upper()
            
            lines.append(f"[{date_str}] [{ticker_label}] [{impact_label}]")
            lines.append(art["title"])
            if include_tldr:
                micro = art.get("ai_micro_summary") if isinstance(art.get("ai_micro_summary"), dict) else {}
                tldr = (micro.get("tldr") or "").strip()
                if tldr:
                    lines.append(f"TL;DR: {tldr}")
            if include_action_score:
                lines.append(f"Action Score: {int(art.get('action_score', 0) or 0)}")
            if include_time_horizon:
                horizon = "unknown"
                micro = art.get("ai_micro_summary") if isinstance(art.get("ai_micro_summary"), dict) else {}
                deep = art.get("ai_deep_analysis") if isinstance(art.get("ai_deep_analysis"), dict) else {}
                if micro.get("time_horizon"):
                    horizon = str(micro.get("time_horizon"))
                elif deep.get("time_horizon"):
                    horizon = str(deep.get("time_horizon"))
                lines.append(f"Time Horizon: {horizon}")
            if art["url"]:
                lines.append(art["url"])
            lines.append("")  # Empty line between articles
        
        txt_content = "\n".join(lines)
        
        # Return as downloadable file
        filename = f"news_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        return PlainTextResponse(
            content=txt_content,
            media_type="text/plain",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )
        
    except Exception as e:
        logger.error(f"Error exporting news: {e}", exc_info=True)
        raise _safe_error()





class NewsAnalysisRequest(BaseModel):
    article_id: Optional[str] = None
    url: Optional[str] = None
    prompt_type: str = "deep"  # deep|summary
    force_refresh: bool = False


def _extract_json_obj(raw_text: str):
    import json
    cleaned = (raw_text or "").replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end >= start:
        cleaned = cleaned[start:end + 1]
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None


NEWS_BRIEFING_SCHEMA_VERSION = 3


def _is_current_news_briefing(data) -> bool:
    if not isinstance(data, dict):
        return False
    try:
        version = int(data.get("briefing_version") or 0)
    except (TypeError, ValueError):
        return False
    ticker_implication = data.get("ticker_implication")
    # A stored placeholder ("unknown" everywhere, written when the model
    # returned nothing usable) is not a briefing: an on-demand request must
    # run the analysis again instead of serving the placeholder from cache.
    if str(data.get("article_explainer") or "").strip().lower() in {"", "unknown"}:
        return False
    if str(data.get("ai_status") or "ok").strip().lower() != "ok":
        return False
    return bool(
        version >= NEWS_BRIEFING_SCHEMA_VERSION
        and str(data.get("article_explainer") or "").strip()
        and str(data.get("market_implication") or "").strip()
        and isinstance(ticker_implication, dict)
        and str(ticker_implication.get("direction") or "").strip()
    )


def _normalize_deep_analysis(data, ticker_label: str):
    payload = data if isinstance(data, dict) else {}

    sentiment = str(payload.get("overall_sentiment", "neutral")).strip().lower()
    if sentiment not in {"bullish", "bearish", "neutral"}:
        sentiment = "neutral"

    try:
        confidence = int(payload.get("confidence", 0))
    except Exception:
        confidence = 0
    confidence = max(0, min(100, confidence))

    impact = str(payload.get("impact_level", "low")).strip().lower()
    if impact not in {"low", "medium", "high"}:
        impact = "low"

    horizon = str(payload.get("time_horizon", "unknown")).strip().lower()
    if horizon not in {"intraday", "1-7d", "1-4w", "long-term", "unknown"}:
        horizon = "unknown"

    def _norm_str_list(values, limit=6):
        if not isinstance(values, list):
            return []
        return [str(x).strip() for x in values if str(x).strip()][:limit]

    def _norm_obj_list(values, fields, limit=5):
        out = []
        if not isinstance(values, list):
            return out
        for item in values[:limit]:
            if not isinstance(item, dict):
                continue
            row = {}
            for f in fields:
                row[f] = str(item.get(f, "unknown")).strip() if item.get(f) is not None else "unknown"
            out.append(row)
        return out

    ticker_payload = payload.get("ticker_implication")
    if not isinstance(ticker_payload, dict):
        ticker_payload = {}
    ticker_name = str(ticker_payload.get("ticker") or ticker_label or "MARKET").strip().upper()[:24] or "MARKET"
    ticker_direction = str(ticker_payload.get("direction", "unclear")).strip().lower()
    if ticker_direction not in {"up", "down", "mixed", "neutral", "unclear"}:
        ticker_direction = "unclear"
    try:
        ticker_confidence = int(ticker_payload.get("confidence", 0))
    except Exception:
        ticker_confidence = 0
    ticker_confidence = max(0, min(100, ticker_confidence))
    direct_link_raw = ticker_payload.get("has_direct_link", ticker_name != "MARKET")
    if isinstance(direct_link_raw, str):
        has_direct_link = direct_link_raw.strip().lower() in {"1", "true", "yes"}
    else:
        has_direct_link = bool(direct_link_raw)

    market_direction = str(payload.get("market_direction", "unclear")).strip().lower()
    if market_direction not in {"risk-on", "risk-off", "mixed", "neutral", "unclear"}:
        market_direction = "unclear"

    evidence_scope = str(payload.get("evidence_scope", "unavailable")).strip().lower()
    if evidence_scope not in {"full-article", "headline-summary", "headline-only", "unavailable"}:
        evidence_scope = "unavailable"

    summary = str(payload.get("summary") or payload.get("overall_summary") or "unknown").strip()
    article_explainer = str(
        payload.get("article_explainer")
        or payload.get("what_happened")
        or summary
        or "unknown"
    ).strip()[:700] or "unknown"
    market_implication = str(
        payload.get("market_implication")
        or payload.get("market_meaning")
        or "unknown"
    ).strip()[:700] or "unknown"

    trade_setups = _norm_obj_list(
        payload.get("trade_setups"),
        ["bias", "entry_trigger", "invalidation", "notes"],
        limit=4,
    )
    for row in trade_setups:
        if row["bias"] not in {"long", "short", "avoid", "observe-only"}:
            row["bias"] = "observe-only"

    trading_ideas = _norm_obj_list(payload.get("trading_ideas"), ["bias", "rationale"], limit=4)
    for row in trading_ideas:
        if row["bias"] not in {"long", "short", "avoid", "observe-only"}:
            row["bias"] = "observe-only"

    normalized = {
        "briefing_version": NEWS_BRIEFING_SCHEMA_VERSION,
        "asset": str(payload.get("asset", ticker_label or "MARKET")),
        "article_explainer": article_explainer,
        "market_implication": market_implication,
        "market_direction": market_direction,
        "evidence_scope": evidence_scope,
        "ticker_implication": {
            "ticker": ticker_name,
            "has_direct_link": has_direct_link and ticker_name != "MARKET",
            "relationship": str(ticker_payload.get("relationship") or "unknown").strip()[:500] or "unknown",
            "direction": ticker_direction,
            "rationale": str(ticker_payload.get("rationale") or "unknown").strip()[:600] or "unknown",
            "confidence": ticker_confidence,
        },
        "overall_sentiment": sentiment,
        "confidence": confidence,
        "impact_level": impact,
        "time_horizon": horizon,
        "price_drivers": _norm_obj_list(payload.get("price_drivers"), ["label", "direction", "details"], limit=6),
        "risks": _norm_obj_list(payload.get("risks"), ["label", "details"], limit=6),
        "key_takeaways": _norm_str_list(payload.get("key_takeaways"), limit=5),
        "trading_ideas": trading_ideas,
        "trade_setups": trade_setups,
        "unknowns": _norm_str_list(payload.get("unknowns"), limit=8),
        "summary": summary or "unknown",
        "overall_summary": str(payload.get("overall_summary") or summary or "unknown"),
    }
    return normalized


@app.get("/api/ollama/status")
def api_ollama_status(refresh: bool = False):
    """Get runtime status plus the dynamically discovered Ollama Cloud catalog."""
    from core.ollama_client import ollama
    available = ollama.check_availability(force=refresh)
    local_models = ollama.get_models(force=refresh) if available else []
    cloud_models = ollama.get_selectable_cloud_models(force=refresh) if available else []
    preferred = ollama.get_preferred_model()
    # Claude models served by the owner's subscription (Claude Code CLI). They
    # are selectable like any other model; calls for them never reach Ollama.
    from core.ai.subscription_routing import subscription_models, subscription_usage
    claude_models = subscription_models()
    best = ollama.get_best_model() if (available or preferred in claude_models) else None
    all_models = list(dict.fromkeys([
        *local_models,
        *cloud_models,
        *claude_models,
        *([preferred] if preferred else []),
    ]))
    return {
        "available": available,
        "models": all_models,
        "local_models": local_models,
        "cloud_models": cloud_models,
        "subscription_models": claude_models,
        "subscription_usage": subscription_usage(),
        "cloud_catalog_available": bool(cloud_models),
        "cloud_catalog_source": "https://ollama.com/api/tags",
        "preferred_model": preferred,
        "active_model": best,
        "timeout": ollama.get_timeout(),
    }


class OllamaTestModelRequest(BaseModel):
    model: str
    prompt: Optional[str] = "Reply with 'OK' and your model name."


@app.post("/api/ollama/test-model")
def api_ollama_test_model(request: OllamaTestModelRequest):
    """Test a specific local or cloud Ollama model on demand."""
    from core.ollama_client import ollama
    import time
    t0 = time.time()
    try:
        # A model test is a quick reachability check: bounded to 60 s so a
        # dead model answers "failed" instead of holding the request for the
        # full background timeout.
        res = ollama.generate(request.prompt, model=request.model, manual_request=True, timeout_override=60)
        dur = round(time.time() - t0, 2)
        if res.get("error"):
            return {"ok": False, "model": request.model, "duration_sec": dur, "error": str(res["error"])}
        return {
            "ok": True,
            "model": request.model,
            "duration_sec": dur,
            "response": str(res.get("response", "")).strip()[:500],
        }
    except Exception as exc:
        dur = round(time.time() - t0, 2)
        return {"ok": False, "model": request.model, "duration_sec": dur, "error": str(exc)}


class OllamaSetModelRequest(BaseModel):
    model: str
    timeout: Optional[int] = None


@app.post("/api/ollama/set-model")
def api_ollama_set_model(request: OllamaSetModelRequest):
    """Set the owner-selected AI model globally and persist it.

    Both existing UI selectors call this endpoint. Keep the legacy Ollama
    keys, the §37 global assignment and router tiers coherent so another AI
    path cannot silently retain a stale selection.
    """
    model = request.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model name is required")

    from core.ollama_client import ollama
    # Allow any local model, dynamically discovered cloud model, or custom name.
    from core.ai.subscription_routing import subscription_provider_name_for
    subscription_name = subscription_provider_name_for(model)
    available_local = ollama.get_models() if ollama.check_availability() else []
    available_cloud = ollama.get_selectable_cloud_models() if not subscription_name else []
    valid_models = set(available_local) | set(available_cloud)
    activation = "already_available" if model in available_local else "not_required"
    if subscription_name:
        # Served by the owner's Claude subscription: nothing to activate in Ollama.
        valid_models.add(model)
        activation = "claude_subscription"
    if not ollama.is_cloud_host() and model in available_cloud and model not in available_local:
        pulled = ollama.pull_model(model)
        if not pulled.get("ok"):
            raise HTTPException(
                status_code=502,
                detail=f"Could not activate Ollama Cloud model '{model}': {pulled.get('error', 'unknown error')}",
            )
        available_local = ollama.get_models(force=True)
        if model not in available_local:
            raise HTTPException(
                status_code=502,
                detail=f"Ollama reported success but '{model}' is not available after activation",
            )
        activation = "cloud_manifest_pulled"
    # Log model switch
    logger.info("Setting global AI model to '%s' (known: %s)", model, model in valid_models)

    # Update ConfigManager so both SQLite-backed runtime config and config.json stay in sync.
    try:
        from core.config import ConfigManager
        cfg = ConfigManager()
        config_data = cfg.config
        if not isinstance(config_data.get("ollama"), dict):
            config_data["ollama"] = {}
        # Write to the key the active host actually reads. On a cloud host
        # `get_preferred_model()` returns `cloud_model or model`, so `cloud_model`
        # wins and writing only `model` would leave the user's selection ignored
        # (regression surfaced by the live Pre-Earnings model selector). Keep the
        # non-active key in sync too so a host flip never resurrects a stale model.
        config_data["ollama"]["model"] = model
        config_data["ollama"]["cloud_model"] = model
        if request.timeout is not None:
            config_data["ollama"]["timeout"] = max(30, min(600, request.timeout))

        ai_config = config_data.setdefault("ai", {})
        assignment = ai_config.setdefault("assignment", {})
        # Roles that simply followed the previous global model move with it.
        # They used to stay behind, so "set the global model" left every
        # analyst on the old one. A role the owner pointed at a different
        # model on purpose (e.g. the critic) keeps its own.
        previous_global = str(assignment.get("all") or "").strip()
        if previous_global and previous_global != model:
            for role_name, role_model in list(assignment.items()):
                if role_name != "all" and str(role_model or "").strip() == previous_global:
                    assignment[role_name] = model
        assignment["all"] = model
        providers = ai_config.setdefault("providers", {})
        ollama_provider = providers.setdefault(
            "ollama", {"enabled": True, "type": "ollama"}
        )
        ollama_provider["default_model"] = model
        if subscription_name and previous_global and not subscription_provider_name_for(previous_global):
            # Remember the Ollama model in use until now: it is what calls fall
            # back to whenever the subscription cannot answer.
            providers.setdefault(subscription_name, {})["fallback_model"] = previous_global

        if cfg.save() is False:
            # The model choice did not reach config.json: say so instead of "ok".
            raise HTTPException(
                status_code=500,
                detail="The AI model selection was NOT saved: the settings file could not be written.",
            )
        ollama.refresh_config()
        # Invalidate the mtime config cache so `get_preferred_model()`/`get_best_model()`
        # see the new model immediately within this same process.
        try:
            from core import ollama_client as _oc
            _oc._CONFIG_CACHE["mtime_ns"] = -1
        except Exception:
            pass

        return {
            "status": "ok",
            "model": model,
            "active_model": ollama.get_best_model(),
            "timeout": config_data["ollama"].get("timeout", 120),
            "scope": "global",
            "assignment": assignment,
            "activation": activation,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving ollama config: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/news/analyze")
def api_analyze_news(payload: NewsAnalysisRequest):
    """
    Deep analyze one article with strict JSON output.
    Uses cache by article_id when available.
    """
    from core.ollama_client import ollama
    from core.content_fetcher import fetch_article_content
    from core.analysis_store import analysis_store
    from agents.news_tab_agent import (
        get_news_tab_agent,
        UNTRUSTED_EVIDENCE_NOTICE,
        wrap_untrusted_evidence,
    )
    from datetime import datetime

    agent = get_news_tab_agent()
    article = agent.get_article_details(payload.article_id) if payload.article_id else None
    article_id = payload.article_id or (article or {}).get("id")
    target_url = payload.url or (article or {}).get("url")

    if not target_url:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing article URL", "detail": "Provide article_id or url."}
        )

    inferred_tickers = agent.infer_briefing_tickers(article or {})
    ticker = str(
        (inferred_tickers[0] if inferred_tickers else None)
        or (article or {}).get("ticker")
        or "MARKET"
    ).strip().upper() or "MARKET"
    title = (article or {}).get("title") or "Unknown Title"
    source = (article or {}).get("source") or "Web"
    published = (article or {}).get("published_at") or datetime.now(timezone.utc).isoformat()
    requested_type = (payload.prompt_type or "deep").strip().lower()
    analysis_type = "deep" if requested_type == "deep" else "micro"

    # 1) Cache hit (article-level first, then legacy URL-level)
    if not payload.force_refresh:
        if article_id:
            cached = analysis_store.get_article_analysis(article_id, analysis_type=analysis_type)
            if cached and _is_current_news_briefing(cached.get("analysis")):
                return {
                    "analysis": cached.get("analysis", {}),
                    "model": cached.get("model", ""),
                    "cached": True,
                    "article_id": article_id,
                    "url": target_url,
                }
        legacy_cached = analysis_store.get_analysis(target_url)
        if (
            legacy_cached
            and isinstance(legacy_cached, dict)
            and legacy_cached.get("analysis_type", "deep") == analysis_type
            and _is_current_news_briefing(legacy_cached.get("analysis"))
        ):
            return {
                "analysis": legacy_cached.get("analysis", {}),
                "model": legacy_cached.get("model", ""),
                "cached": True,
                "article_id": article_id,
                "url": target_url,
            }

    # 2) Ollama checks — with fallback from pre-computed article metadata
    ollama_available = ollama.check_availability()
    if not ollama_available:
        if article and isinstance(article, dict):
            sentiment = article.get("sentiment", "neutral")
            summary = article.get("summary") or article.get("title") or "No summary available."
            confidence = int((article.get("sentiment_confidence") or 0) * 100)
            impact = article.get("impact", "low")
            direction = "up" if sentiment in {"bullish", "very_bullish"} else (
                "down" if sentiment in {"bearish", "very_bearish"} else "unclear"
            )
            direct_link = ticker != "MARKET"
            # Tab audit 2026-11-15: this fallback used to paste the raw source
            # text (a whole social post) into "article_explainer" and
            # "key_takeaways", so the UI presented it under "WHAT IS THE NEWS?"
            # as if it were an AI answer. No AI ran here: say so explicitly and
            # hand the source text over as a labelled excerpt instead.
            unavailable_note = (
                "AI briefing unavailable: the AI model is not reachable, so this "
                "item has not been analysed. Only source metadata is shown."
            )
            fallback_analysis = {
                "briefing_version": NEWS_BRIEFING_SCHEMA_VERSION,
                "ai_status": "unavailable",
                "ai_unavailable_reason": "AI model is not reachable",
                "source_excerpt": str(summary or "")[:500],
                "article_explainer": unavailable_note,
                "market_implication": (
                    f"Metadata classifies this as {str(impact).upper()} impact with a "
                    f"{str(sentiment).replace('_', ' ')} signal; full article analysis is unavailable."
                ),
                "market_direction": "unclear",
                "evidence_scope": "headline-summary",
                "ticker_implication": {
                    "ticker": ticker,
                    "has_direct_link": direct_link,
                    "relationship": (
                        f"The source metadata associates this article with {ticker}."
                        if direct_link else "No direct ticker relationship is present in the source metadata."
                    ),
                    "direction": direction if direct_link else "unclear",
                    "rationale": "Direction is inferred only from pre-computed article sentiment because AI text analysis is unavailable.",
                    "confidence": confidence,
                },
                "overall_sentiment": sentiment if isinstance(sentiment, str) else "neutral",
                "confidence": confidence,
                "impact_level": impact if isinstance(impact, str) else "low",
                "key_takeaways": [],
                "summary": unavailable_note,
                "overall_summary": unavailable_note,
                "source": "pre_computed_metadata",
            }
            return {
                "analysis": fallback_analysis,
                "model": "pre_computed_metadata",
                "cached": False,
                "fallback": True,
                "article_id": article_id,
                "url": target_url,
            }
        return JSONResponse(
            status_code=503,
            content={
                "error": "Ollama is not reachable.",
                "detail": "Please ensure Ollama is running (localhost:11434).",
                "fallback_available": False,
            }
        )

    from api.state_service import get_shared_config
    from core.ai.role_resolution import assigned_model

    # The owner's model for news analysis; the global model when the role has none.
    model = assigned_model("news_analyst", config=get_shared_config(), fallback=ollama.get_best_model())
    if not model:
        return JSONResponse(
            status_code=400,
            content={
                "error": "No AI models found.",
                "detail": "Run 'ollama pull gemma3:27b' in your terminal."
            }
        )

    # 3) Fetch article content
    text = fetch_article_content(target_url, max_length=7000)
    evidence_scope = "full-article"
    if not text or str(text).lstrip().startswith("["):
        if not article:
            return JSONResponse(
                status_code=400,
                content={"error": "Could not extract text from URL and no article metadata is available."}
            )
        provider_summary = str((article or {}).get("summary") or "").strip()
        text = f"Headline: {title}\nProvider summary: {provider_summary or 'unavailable'}"
        evidence_scope = "headline-summary" if provider_summary else "headline-only"

    context = text[:6000]

    system_prompt = "You are a financial/crypto analyst. Output ONLY strict JSON."
    deep_prompt = (
        "Use ONLY the provided article text. Do NOT add external facts.\n"
        "If information is missing, write 'unknown'.\n"
        "Answer three essential questions explicitly: (1) what the news actually is, "
        "(2) what it means for the broader market, and (3) whether there is a causal ticker link "
        "and the likely up/down/mixed direction. The direction is probabilistic, not guaranteed.\n"
        "If Ticker is MARKET, set has_direct_link=false unless the article itself names a specific asset.\n"
        "Output ONLY one JSON object with exact keys:\n"
        "{"
        f"\"briefing_version\":{NEWS_BRIEFING_SCHEMA_VERSION},"
        f"\"evidence_scope\":\"{evidence_scope}\","
        "\"asset\":\"string\","
        "\"article_explainer\":\"one or two sentences stating the actual news\","
        "\"market_implication\":\"one or two sentences explaining the broader market consequence and mechanism\","
        "\"market_direction\":\"risk-on|risk-off|mixed|neutral|unclear\","
        "\"ticker_implication\":{"
        "\"ticker\":\"ticker or MARKET\","
        "\"has_direct_link\":true,"
        "\"relationship\":\"how the news affects the named company or asset\","
        "\"direction\":\"up|down|mixed|neutral|unclear\","
        "\"rationale\":\"why that direction follows from the article\","
        "\"confidence\":0},"
        "\"overall_sentiment\":\"bullish|bearish|neutral\","
        "\"confidence\":0,"
        "\"impact_level\":\"low|medium|high\","
        "\"time_horizon\":\"intraday|1-7d|1-4w|long-term|unknown\","
        "\"price_drivers\":[{\"label\":\"\",\"direction\":\"bullish|bearish|mixed|unclear\",\"details\":\"\"}],"
        "\"risks\":[{\"label\":\"\",\"details\":\"\"}],"
        "\"key_takeaways\":[\"...\",\"...\",\"...\"],"
        "\"trading_ideas\":[{\"bias\":\"long|short|avoid|observe-only\",\"rationale\":\"\"}],"
        "\"trade_setups\":[{\"bias\":\"long|short|avoid|observe-only\",\"entry_trigger\":\"\",\"invalidation\":\"\",\"notes\":\"\"}],"
        "\"unknowns\":[\"...\"],"
        "\"summary\":\"string\","
        "\"overall_summary\":\"string\""
        "}\n"
        f"Ticker: {ticker}\n"
        f"Published: {published}\n"
        f"Evidence scope: {evidence_scope}\n"
        f"{UNTRUSTED_EVIDENCE_NOTICE}\n"
        + wrap_untrusted_evidence(
            f"Title: {title}\nSource: {source}\nArticle text:\n{context}"
        )
    )

    summary_prompt = (
        "Use ONLY the article text; do not add external facts. Answer the three essential "
        "questions directly and concisely. Direction is probabilistic, not guaranteed. "
        "If Ticker is MARKET, set has_direct_link=false unless a specific asset is named.\n"
        "Output ONLY JSON:\n"
        "{"
        f"\"briefing_version\":{NEWS_BRIEFING_SCHEMA_VERSION},"
        f"\"evidence_scope\":\"{evidence_scope}\","
        "\"tldr\":\"max 120 chars\","
        "\"article_explainer\":\"what the news actually is\","
        "\"market_implication\":\"what it means for the broader market and why\","
        "\"market_direction\":\"risk-on|risk-off|mixed|neutral|unclear\","
        "\"ticker_implication\":{"
        "\"ticker\":\"ticker or MARKET\","
        "\"has_direct_link\":true,"
        "\"relationship\":\"causal relationship or no direct link\","
        "\"direction\":\"up|down|mixed|neutral|unclear\","
        "\"rationale\":\"article-based reason\","
        "\"confidence\":0},"
        "\"key_takeaways\":[\"...\",\"...\",\"...\"],"
        "\"overall_sentiment\":\"bullish|bearish|neutral\","
        "\"confidence\":0,"
        "\"time_horizon\":\"intraday|1-7d|1-4w|long-term|unknown\","
        "\"actionability\":0,"
        "\"what_to_watch\":[\"...\",\"...\"]"
        "}\n"
        f"Ticker: {ticker}\n"
        f"Evidence scope: {evidence_scope}\n"
        f"{UNTRUSTED_EVIDENCE_NOTICE}\n"
        + wrap_untrusted_evidence(f"Title: {title}\nArticle text:\n{context}")
    )

    prompt = deep_prompt if requested_type == "deep" else summary_prompt
    # Explicit read timeout for the news briefing (config news.ai_briefing_timeout_seconds,
    # default 90 s) instead of the global Ollama timeout, so one slow model call
    # (121 s observed) fails with a clear message rather than hanging the panel.
    try:
        briefing_timeout = int((getattr(agent, "_news_cfg", {}) or {}).get("ai_briefing_timeout_seconds", 90) or 90)
    except Exception:
        briefing_timeout = 90
    briefing_timeout = max(15, min(300, briefing_timeout))
    result = ollama.generate(
        prompt,
        model=model,
        system=system_prompt,
        timeout_override=briefing_timeout,
    )
    if "error" in result:
        return JSONResponse(
            status_code=502,
            content={
                "error": "AI briefing unavailable: the AI model did not answer.",
                "detail": str(result.get("error") or "")[:300],
                "ai_status": "unavailable",
            },
        )

    raw_response = (result.get("response") or "").strip()
    parsed = _extract_json_obj(raw_response)
    if not isinstance(parsed, dict) or not str(
        parsed.get("article_explainer") or parsed.get("what_happened")
        or parsed.get("summary") or parsed.get("tldr") or ""
    ).strip():
        # The model answered, but not with the briefing schema. There is no
        # analysis to show: report that, and do not cache a placeholder (it was
        # stored as a valid briefing full of "unknown" and served from cache).
        logger.warning("News briefing rejected: model returned no usable JSON (article_id=%s)", article_id)
        return JSONResponse(
            status_code=502,
            content={
                "error": "AI briefing unavailable: the model returned no usable analysis. Try again.",
                "ai_status": "invalid_output",
                "article_id": article_id,
                "url": target_url,
            },
        )

    if requested_type == "deep":
        normalized = _normalize_deep_analysis(parsed, ticker_label=str(ticker))
        normalized["ai_status"] = "ok"
    else:
        normalized = agent._normalize_micro_summary(parsed)

    try:
        if article_id:
            analysis_store.save_article_analysis(
                article_id=article_id,
                analysis_data=normalized,
                model=model,
                analysis_type=analysis_type,
                url=target_url,
            )
        else:
            analysis_store.save_analysis(target_url, normalized, model, analysis_type=analysis_type)
    except Exception as e:
        logger.error(f"Failed to save news analysis: {e}")

    # Keep the validated briefing on the in-memory article as well, so the
    # next state poll still shows it (it used to vanish until a re-open).
    attached_via = _attach_news_briefing(
        agent, article_id, normalized, analysis_type, str(result.get("model") or model)
    )

    return {
        "analysis": normalized,
        "raw": raw_response,
        "model": result.get("model", model),
        "done": True,
        "cached": False,
        "attached": bool(attached_via),
        "article_id": article_id,
        "url": target_url,
    }


# --- Backtest Testing Agent ---





# --- US Market News Agent ---
@app.get("/api/us-market-news/state")
def api_us_market_news_state():
    """Get full US market news state"""
    try:
        from agents.us_market_news_agent import get_us_market_news_agent
        agent = get_us_market_news_agent()
        return agent.get_state()
    except Exception as e:
        logger.error(f"Error getting US market news: {e}", exc_info=True)
        raise _safe_error()

@app.get("/api/us-market-news/today")
def api_us_market_news_today():
    """Get today's economic events"""
    try:
        from agents.us_market_news_agent import get_us_market_news_agent
        agent = get_us_market_news_agent()
        return {"events": agent.get_today_events()}
    except Exception as e:
        logger.error(f"Error getting today's events: {e}", exc_info=True)
        raise _safe_error()

@app.get("/api/us-market-news/week")
def api_us_market_news_week():
    """Get this week's economic events"""
    try:
        from agents.us_market_news_agent import get_us_market_news_agent
        agent = get_us_market_news_agent()
        return {"events": agent.get_this_week_events()}
    except Exception as e:
        logger.error(f"Error getting week's events: {e}", exc_info=True)
        raise _safe_error()

@app.get("/api/us-market-news/high-impact")
def api_us_market_news_high_impact(days: int = 30):
    """Get high impact events for next N days"""
    try:
        from agents.us_market_news_agent import get_us_market_news_agent
        agent = get_us_market_news_agent()
        return {"events": agent.get_high_impact_events(days)}
    except Exception as e:
        logger.error(f"Error getting high impact events: {e}", exc_info=True)
        raise _safe_error()

@app.get("/api/us-market-news/fed")
def api_us_market_news_fed():
    """Get Fed/FOMC schedule"""
    try:
        from agents.us_market_news_agent import get_us_market_news_agent
        agent = get_us_market_news_agent()
        return {
            "schedule": agent.get_fed_schedule(),
            "next_meeting": agent.get_next_fed_meeting()
        }
    except Exception as e:
        logger.error(f"Error getting Fed schedule: {e}", exc_info=True)
        raise _safe_error()

@app.get("/api/us-market-news/breaking")
def api_us_market_news_breaking(max_age_hours: int = 4):
    """Get breaking market news"""
    try:
        from agents.us_market_news_agent import get_us_market_news_agent
        agent = get_us_market_news_agent()
        return {"news": agent.get_breaking_news(max_age_hours)}
    except Exception as e:
        logger.error(f"Error getting breaking news: {e}", exc_info=True)
        raise _safe_error()

@app.post("/api/us-market-news/refresh")
def api_us_market_news_refresh():
    """Force refresh all US market news data"""
    try:
        from agents.us_market_news_agent import get_us_market_news_agent
        agent = get_us_market_news_agent()
        return agent.refresh()
    except Exception as e:
        logger.error(f"Error refreshing US market news: {e}", exc_info=True)
        raise _safe_error()

# --- Economic Calendar ---
def _macro_calendar_path(year):
    """Path of the bundled US macro-release schedule for one year."""
    import os
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, "data", f"us_macro_calendar_{year}.json")


def _merge_macro_calendar_events(start_date, end_date, econ_list):
    """Append US macro releases (CPI/PCE/Jobs Report/Retail Sales/GDP/PPI) from
    data/us_macro_calendar_*.json into ``econ_list`` (dedup by id).

    These are fixed published dates from the OMB/OIRA "Principal Federal Economic
    Indicators" schedule — no API key, no first-Friday guessing (BLS shifts for
    Jan/May/Jul holidays). Shared by /api/calendar/events AND /api/calendar/range
    so both endpoints surface the same macro calendar. Pre-fix this block lived
    only inside /range, so the Calendar modal (which reads /events) showed zero
    macro events — a "missing live data" gap (directive: every tab live/correct)."""
    try:
        import os
        import json as _json
        from datetime import datetime as _dt, timezone as _timezone
        from zoneinfo import ZoneInfo as _ZoneInfo
        existing = {e.get("id") for e in econ_list if isinstance(e, dict)}
        for yr in sorted({start_date.year, end_date.year}):
            macro_path = _macro_calendar_path(yr)
            if not os.path.exists(macro_path):
                continue
            with open(macro_path, "r", encoding="utf-8") as mf:
                macro = _json.load(mf)
            default_et = str(macro.get("time_et", "08:30"))
            for entry in macro.get("events", []):
                title = entry.get("title", "US Economic Release")
                impact = entry.get("impact", "high")
                et_label = str(entry.get("time_et", default_et))
                for ds in entry.get("dates", []):
                    try:
                        ed = _dt.strptime(ds, "%Y-%m-%d")
                    except Exception:
                        continue
                    if not (start_date <= ed <= end_date):
                        continue
                    eid = f"macro_{entry.get('indicator','evt')}_{ed.strftime('%Y%m%d')}"
                    if eid in existing:
                        continue
                    hours, minutes = map(int, et_label.split(":"))
                    release = ed.replace(hour=hours, minute=minutes, tzinfo=_ZoneInfo("America/New_York"))
                    econ_list.append({
                        "id": eid,
                        "title": title,
                        "date": release.astimezone(_timezone.utc).isoformat(),
                        "date_formatted": f"{ds} {et_label} ET",
                        "category": "economic",
                        "impact": impact,
                        "country": "US",
                        "source_name": str(macro.get("source") or "Published official macro-release schedule"),
                        "source_url": str(entry.get("source_url") or macro.get("source_url") or ""),
                        "data_provenance": "published_schedule",
                        "time_precision": "published_time",
                    })
                    existing.add(eid)
    except Exception as _e:
        logger.debug(f"Calendar macro-events enrichment skipped: {_e}")


def _macro_calendar_coverage(start_date, end_date):
    """Which years of the bundled macro-release schedule exist for a range.

    The schedule is one hand-maintained file per year (data/us_macro_calendar_
    <year>.json). When a year is missing the Calendar used to render those
    months with no CPI/Jobs/PCE at all, indistinguishable from "nothing is
    scheduled" — January 2027 on 2026-11-15. The UI needs to say so.

    A year file can also be partial: an agency that has published only some
    months is stored with ``"complete": false`` (and a ``note``) on that
    indicator. That is passed through per indicator, so the Calendar can say
    "GDP and PCE through June" instead of looking complete. A file that cannot
    be read counts as missing (its events are not shown either)."""
    import os
    import json as _json
    available, missing, unreadable, partial = [], [], [], []
    schedules, partial_notes = [], []
    for yr in range(start_date.year, end_date.year + 1):
        path = _macro_calendar_path(yr)
        if not os.path.exists(path):
            missing.append(yr)
            continue
        try:
            with open(path, "r", encoding="utf-8") as mf:
                macro = _json.load(mf)
            if not isinstance(macro, dict) or not isinstance(macro.get("events"), list):
                raise ValueError("no events list")
        except Exception as _e:
            logger.warning(f"Macro calendar file for {yr} is unreadable: {_e}")
            missing.append(yr)
            unreadable.append(yr)
            continue
        available.append(yr)
        schedule = _macro_year_schedule(yr, macro)
        schedules.append(schedule)
        if not schedule["complete"]:
            partial.append(yr)
            partial_notes.append(
                f"{schedule['summary']} Later dates are not published by the agencies yet — "
                "an empty month there does not mean nothing is scheduled."
            )
    return {
        "years_available": available,
        "years_missing": missing,
        "years_unreadable": unreadable,
        "years_partial": partial,
        "complete": not missing and not partial,
        "schedules": schedules,
        "partial_notes": partial_notes,
    }


_MACRO_INDICATOR_LABELS = {
    "jobs": "Jobs", "cpi": "CPI", "pce": "PCE",
    "retail": "Retail Sales", "ppi": "PPI", "gdp": "GDP",
}


def _macro_year_schedule(year, macro):
    """Per-indicator publication status of one year's macro schedule file.

    ``complete`` is the file's own flag (absent = complete, as in the 2026
    file); an indicator without any valid date is never complete.
    ``published_through`` is the last release date actually in the file —
    nothing is inferred beyond it."""
    from datetime import datetime as _dt
    indicators, groups = [], {}
    for entry in macro.get("events", []) or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("indicator") or "evt")
        valid = []
        for ds in entry.get("dates", []) or []:
            try:
                valid.append(_dt.strptime(str(ds), "%Y-%m-%d").date())
            except Exception:
                continue
        last = max(valid) if valid else None
        label = _MACRO_INDICATOR_LABELS.get(key) or str(entry.get("title") or key)
        indicators.append({
            "indicator": key,
            "label": label,
            "complete": bool(valid) and entry.get("complete", True) is True,
            "published_through": last.isoformat() if last else None,
            "dates_count": len(valid),
            "note": str(entry.get("note") or ""),
            "source_url": str(entry.get("source_url") or macro.get("source_url") or ""),
            "retrieved": str(entry.get("retrieved") or ""),
        })
        groups.setdefault(last.month if last else 0, []).append(label)

    def _join(labels):
        return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]

    parts = []
    for month in sorted(groups, reverse=True):
        names = _join(groups[month])
        if not month:
            parts.append(f"{names} not published yet")
        else:
            month_name = _dt(year, month, 1).strftime("%B")
            parts.append(f"{names} {'through' if parts else 'published through'} {month_name}")
    summary = f"US macro schedule for {year}: " + ("; ".join(parts) if parts else "no indicators listed") + "."
    return {
        "year": year,
        "complete": bool(indicators) and all(item["complete"] for item in indicators),
        "summary": summary,
        "indicators": indicators,
    }


@app.get("/api/calendar/events")
def api_calendar_events(days: int = 14, refresh: bool = False):
    """Get economic calendar events for the next N days - cache first, optional remote refresh"""
    try:
        ctx = get_request_context()
        forced_cache = bool(ctx and ctx.cache_only)
        if not refresh or forced_cache:
            payload = _calendar_events_cached(days=days, ttl_seconds=60.0)
            if payload is not None:
                cache_manager.record_manual_decision(
                    key=f"calendar-events:{int(days)}",
                    decision="HIT",
                    reason="endpoint_payload_cache",
                    source="calendar_events_endpoint",
                    ttl_seconds=60,
                    age_seconds=None,
                )
                return Response(content=payload, media_type="application/json")

        from agents.economic_calendar_agent import get_economic_calendar_agent

        agent = get_economic_calendar_agent()
        cal_enabled = _agent_enabled("economic_calendar")
        allow_remote = bool(refresh and cal_enabled and not forced_cache)

        response_payload = {
            "upcoming": agent.get_upcoming_events(days, allow_remote=allow_remote),
            "fed_schedule": agent.get_fed_schedule(),
            "high_impact": agent.get_high_impact_events(days, allow_remote=allow_remote),
            "next_fed": agent.get_next_fed_meeting(),
            "disabled": not cal_enabled,
            "from_cache": not allow_remote,
        }

        # Macro enrichment (CPI/PCE/Jobs/Retail/GDP/PPI) — same shared helper as
        # /api/calendar/range. Pre-fix /events returned only the agent's economic
        # list (FMP/FinnHub-driven, mostly empty post-removal) and the Calendar
        # modal showed zero macro releases. Mirror the agent's own window
        # (datetime.now() + N days) so the two endpoints agree.
        try:
            from datetime import datetime as _dt, timedelta as _td
            _today = _dt.now().date()
            _start = _dt(_today.year, _today.month, _today.day)
            _end = _start + _td(days=int(days))
            _upcoming = response_payload.get("upcoming")
            if isinstance(_upcoming, dict):
                _merge_macro_calendar_events(_start, _end, _upcoming.setdefault("economic", []))
        except Exception:
            pass

        payload = json.dumps(response_payload, ensure_ascii=False, default=str).encode("utf-8")
        _set_calendar_events_cache(days=days, payload=payload)

        cache_manager.record_manual_decision(
            key=f"calendar-events:{int(days)}",
            decision="HIT" if not allow_remote else "MISS",
            reason=("cache_only_policy" if not allow_remote else "remote_refresh_requested"),
            source="calendar_events_endpoint",
            ttl_seconds=60,
            age_seconds=None,
        )
        return Response(content=payload, media_type="application/json")
    except Exception as e:
        logger.error(f"Error getting calendar events: {e}", exc_info=True)
        raise _safe_error()

@app.get("/api/calendar/range")
def api_calendar_range(start: str, end: str, refresh: bool = False, all_reporters: bool = True):
    """Get calendar events for a date range (YYYY-MM-DD).

    all_reporters=True (default): earnings list includes ALL US reporters in
    range (FinnHub /calendar/earnings bulk fetch), not just the watchlist —
    fixes the "only a few earnings show" symptom (Faza 2, 2026-07-07).
    """
    try:
        from agents.economic_calendar_agent import get_economic_calendar_agent
        from datetime import datetime
        
        # Parse dates
        try:
            start_date = datetime.strptime(start, "%Y-%m-%d")
            end_date = datetime.strptime(end, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        # A reversed range silently returned an empty calendar ("no events"),
        # and an unbounded one walks every day of the span under the cache lock.
        if end_date < start_date:
            raise HTTPException(status_code=400, detail="Invalid range: end is before start")
        if (end_date - start_date).days > 400:
            raise HTTPException(status_code=400, detail="Range too large (max 400 days)")

        agent = get_economic_calendar_agent()

        # Get watchlist tickers first for Hybrid Fetch
        watchlist_tickers = []
        try:
            import json
            import os
            # The live watchlist file of this instance (MS_WATCHLIST_PATH /
            # runtime folder), not the legacy <repo>/watchlist.json copy that
            # stopped being written when state moved to runtime/.
            from core.config import WATCHLIST_PATH as _calendar_watchlist_path
            watchlist_path = str(_calendar_watchlist_path)
            
            if os.path.exists(watchlist_path):
                with open(watchlist_path, "r") as f:
                    data = json.load(f)
                    stocks = data.get("stocks", [])
                    hot_list = data.get("hot_list", [])
                    watchlist_tickers = [str(s).upper() for s in (stocks + hot_list)]
            
            if not watchlist_tickers:
                # Try Engine as second choice if file read failed or returned empty
                from api.engine_service import get_engine
                engine = get_engine()
                if engine and hasattr(engine, 'state_cache'):
                    state = engine.state_cache
                    if "tickers" in state:
                        tickers = state.get("tickers")
                        if isinstance(tickers, dict):
                            watchlist_tickers = list(tickers.keys())
                        elif isinstance(tickers, list):
                            watchlist_tickers = [t.get("symbol", "") for t in tickers if isinstance(t, dict) and t.get("symbol")]
            if not watchlist_tickers:
                logger.warning("Calendar API: Could not find any watchlist tickers via file or engine.")
            # Ensure unique and uppercase
            watchlist_tickers = list(set([str(t).upper() for t in watchlist_tickers if t]))
            logger.info(f"API Calendar: Loaded {len(watchlist_tickers)} watchlist tickers")
            
        except Exception as e:
            logger.warning(f"Could not get watchlist for calendar: {e}")

        # Pass watchlist to agent for Hybrid Fetch
        cal_enabled = _agent_enabled("economic_calendar")
        ctx = get_request_context()
        forced_cache = bool(ctx and ctx.cache_only)
        allow_remote = bool(refresh and cal_enabled and not forced_cache)
        calendar_data = agent.get_calendar_events_range(
            start_date,
            end_date,
            watchlist_tickers=watchlist_tickers,
            allow_remote=allow_remote,
            include_all_reporters=all_reporters,
        )
        
        # Enrich earnings from yfinance (no API key) for watchlist stocks — the FMP/FinnHub
        # removal left the agent's earnings empty, so the calendar only showed Fed events.
        # Uses the cached resolver (warmed by the pre-earnings/hotlist flows); cache-only here
        # to keep the endpoint fast, with a background warm on miss.
        try:
            from api.state_service import _resolve_earnings_from_yfinance
            from core.config import is_crypto_ticker, get_bucharest_time
            today = get_bucharest_time().date()
            earn_list = calendar_data.setdefault("earnings", [])
            existing = {str(e.get("symbol", "")).upper() for e in earn_list if isinstance(e, dict)}
            # Tab audit 2026-11-15: a watchlist ticker that already reported
            # inside the range (e.g. AAPL on 2 Nov) was skipped here, so its
            # NEXT report never reached the calendar. Skip only tickers that
            # already have a row on/after today.
            today_iso = today.isoformat()
            existing_upcoming = {
                str(e.get("symbol", "")).upper() for e in earn_list
                if isinstance(e, dict) and str(e.get("date") or e.get("date_formatted") or "")[:10] >= today_iso
            }
            for tk in watchlist_tickers:
                tk = str(tk).upper().strip()
                if not tk or tk in existing_upcoming or is_crypto_ticker(tk):
                    continue
                date_str, _days, _when, _wd = _resolve_earnings_from_yfinance(tk, today, allow_remote=allow_remote)
                if not date_str or date_str == "N/A":
                    continue
                try:
                    ed = datetime.strptime(date_str, "%Y-%m-%d")
                except Exception:
                    continue
                if ed.date() < today and tk in existing:
                    continue  # stale cached "next" date; the past report is already listed
                if start_date <= ed <= end_date:
                    earn_list.append({
                        "id": f"earn_{tk}_{date_str.replace('-', '')}",
                        "title": f"{tk} Earnings",
                        "symbol": tk,
                        "date": ed.strftime("%Y-%m-%dT00:00:00"),
                        "date_formatted": date_str,
                        "category": "earnings",
                        "impact": "high",
                        # Say where the date comes from and that the session
                        # is not known — the row used to carry neither.
                        "when": "unknown",
                        "when_desc": "Time Unconfirmed",
                        "source": "yfinance",
                        "source_url": f"https://finance.yahoo.com/quote/{tk}/analysis/",
                        "confirmed": False,
                        "date_confidence": "medium",
                        "session_confidence": "unknown",
                        "is_upcoming": ed.date() >= today,
                    })
                    existing.add(tk)
                    existing_upcoming.add(tk)
        except Exception as _e:
            logger.debug(f"Calendar yfinance earnings enrichment skipped: {_e}")

        # Add US macro releases (CPI, PCE, Jobs Report, Retail Sales, GDP, PPI) from the
        # official OMB/OIRA "Principal Federal Economic Indicators" schedule (data/us_macro_
        # calendar_*.json). Fixed published dates — no API key, no guessing. FOMC decisions
        # come separately from the economic_calendar_agent. Shared helper keeps /events and
        # /range in sync (directive: every tab live/correct, nothing missing).
        _merge_macro_calendar_events(start_date, end_date, calendar_data.setdefault("economic", []))

        # Log findings for debug
        earn_count = len(calendar_data.get("earnings", []))
        logger.info(f"API Calendar: Found {earn_count} earnings for range {start} to {end}")

        # 2026-09-03 audit: the 3-month grid highlights rows via ev.is_watchlist,
        # but bulk earnings rows were stored with is_watchlist=False and this
        # endpoint never annotated them — the highlight could never fire.
        # Tab audit 2026-11-15: the flag is now set BOTH ways — a cached row
        # kept is_watchlist=True (and its star) after the ticker was removed
        # from the watchlist.
        _watch_set = set(watchlist_tickers)
        for row in calendar_data.get("earnings", []) or []:
            if isinstance(row, dict):
                row["is_watchlist"] = str(row.get("symbol") or "").upper() in _watch_set

        # Attach to response for frontend sorting
        calendar_data["watchlist_tickers"] = watchlist_tickers
        calendar_data["disabled"] = not cal_enabled
        # Honest coverage/freshness block: data age, how far the earnings list
        # really reaches, macro-schedule years on disk, IPO feed availability.
        try:
            coverage = agent.get_calendar_coverage()
            coverage["macro"] = _macro_calendar_coverage(start_date, end_date)
            coverage["agent_disabled"] = not cal_enabled
            calendar_data["coverage"] = coverage
        except Exception as _cov_err:
            logger.debug(f"Calendar coverage block skipped: {_cov_err}")
        cache_manager.record_manual_decision(
            key=f"calendar-range:{start}:{end}",
            decision="HIT" if not allow_remote else "MISS",
            reason=("cache_only_policy" if not allow_remote else "remote_refresh_requested"),
            source="calendar_range_endpoint",
            ttl_seconds=300,
            age_seconds=None,
        )
        return calendar_data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting calendar range: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/calendar/today")
def api_calendar_today(refresh: bool = False):
    """Get today's economic events"""
    try:
        from agents.economic_calendar_agent import get_economic_calendar_agent
        agent = get_economic_calendar_agent()
        cal_enabled = _agent_enabled("economic_calendar")
        ctx = get_request_context()
        forced_cache = bool(ctx and ctx.cache_only)
        allow_remote = bool(refresh and cal_enabled and not forced_cache)
        cache_manager.record_manual_decision(
            key="calendar-today",
            decision="HIT" if not allow_remote else "MISS",
            reason=("cache_only_policy" if not allow_remote else "remote_refresh_requested"),
            source="calendar_today_endpoint",
            ttl_seconds=120,
            age_seconds=None,
        )
        return {
            "events": agent.get_today_events(allow_remote=allow_remote),
            "disabled": not cal_enabled,
        }
    except Exception as e:
        logger.error(f"Error getting today's events: {e}", exc_info=True)
        raise _safe_error()



# --- Scanner Agent ---
def _maybe_hint_trade_radar_refresh() -> None:
    global _RADAR_HINT_TS
    now_ts = time.time()
    with _RADAR_HINT_LOCK:
        if (now_ts - float(_RADAR_HINT_TS or 0.0)) < 15.0:
            return
        _RADAR_HINT_TS = now_ts
    try:
        from agents.trade_radar_agent import get_trade_radar_agent_if_ready

        radar = get_trade_radar_agent_if_ready()
        if radar is not None and _agent_enabled("trade_radar"):
            radar.request_refresh(force=False)
    except Exception:
        pass


def _scanner_snapshot_superseded(cached_data: Dict[str, Any]) -> bool:
    """True when the scanner has finished a newer scan than the cached payload.

    The 20 s endpoint cache is cleared when a manual refresh is *queued*, but
    the next poll re-cached the pre-refresh snapshot, so "Refresh now" kept
    returning the old list until the TTL ran out.
    """
    try:
        from agents import scanner_agent as _scanner_module

        agent = getattr(_scanner_module, "_scanner_agent_instance", None)
        latest = getattr(agent, "_last_success_dt", None) if agent is not None else None
        if latest is None:
            return False
        return _scanner_module._iso(latest) != cached_data.get("as_of_utc")
    except Exception:
        return False


def _api_scanner_state_impl():
    cached = _endpoint_payload_get("scanner_state", ttl_seconds=20.0)
    if cached is not None:
        try:
            cached_data = json.loads(cached)
            if (
                isinstance(cached_data, dict)
                and cached_data.get("hot_tickers")
                and not _scanner_snapshot_superseded(cached_data)
            ):
                return Response(content=cached, media_type="application/json")
        except Exception:
            return Response(content=cached, media_type="application/json")
    ctx = get_request_context()
    forced_cache = bool(ctx and ctx.cache_only)
    if os.getenv("MS_DISABLE_SCANNER_AGENT", "0") == "1":
        return {
            "hot_tickers": [],
            "refreshing": False,
            "status": {"state": "TEST_ISOLATED"},
            "flags": {"refreshing": False, "data_available": False},
            "data_age_seconds": None,
            "as_of_utc": None,
            "refresh_seconds": 0,
        }
    from agents.scanner_agent import get_scanner_agent
    agent = get_scanner_agent()
    data = agent.get_state()
    # get_state() skips background refresh when request context has cache_only=True.
    # Explicitly nudge a refresh if empty; this is non-blocking and keeps the UI
    # from getting stuck on a cached empty scanner forever.
    lazy_refresh_disabled = os.getenv("MS_DISABLE_SCANNER_LAZY_REFRESH", "0") == "1"
    if (
        not forced_cache
        and not data.get("hot_tickers")
        and not data.get("refreshing")
        and not lazy_refresh_disabled
    ):
        try:
            agent.request_refresh()
            data["refreshing"] = True
        except Exception:
            pass
    # Tag status as WARMING_UP when no data yet
    if not data.get("hot_tickers"):
        st = data.get("status")
        if isinstance(st, dict):
            st["state"] = "WARMING_UP"
        else:
            data["status"] = {"state": "WARMING_UP"}
    if not forced_cache:
        _maybe_hint_trade_radar_refresh()
    _set_ctx_cache_reason("MISS")
    payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    if data.get("hot_tickers"):
        _endpoint_payload_set("scanner_state", payload)
    return Response(content=payload, media_type="application/json")


@app.get("/api/scanner/state")
def api_scanner_state():
    """Get active scanner hot tickers"""
    try:
        return _api_scanner_state_impl()
    except Exception as e:
        logger.error(f"Error getting scanner state: {e}")
        # Report the failure: a bare empty list renders as "no movers detected".
        return {
            "hot_tickers": [],
            "refreshing": False,
            "status": {"state": "ERROR", "last_error": "Scanner state unavailable (server error)"},
            "flags": {"refreshing": False, "data_available": False},
            "data_age_seconds": None,
            "as_of_utc": None,
        }


@app.post("/api/scanner/refresh")
def api_scanner_refresh():
    """Trigger non-blocking scanner refresh and invalidate scanner endpoint cache."""
    try:
        from agents.scanner_agent import get_scanner_agent

        agent = get_scanner_agent()
        if hasattr(agent, "request_refresh"):
            result = agent.request_refresh()
            _endpoint_payload_clear("scanner_state")
            return {"status": "accepted", "request": result, "async": True}
        data = agent.run_scan()
        _endpoint_payload_clear("scanner_state")
        return {"status": "ok", "hot_tickers": data}
    except Exception as e:
        logger.error(f"Error refreshing scanner: {e}", exc_info=True)
        raise _safe_error()

# --- API Stats ---
@app.get("/api/stats/api-requests")
def api_get_api_stats():
    """Get current API request stats + 3-minute history"""
    try:
        from core.api_stats_logger import get_api_stats_history

        req_metrics = snapshot_request_metrics()
        history = get_api_stats_history()
        return {
            "current": {
                "fmp_rpm": int(req_metrics.get("external_calls_by_service_last_minute", {}).get("fmp", 0)),
                "yahoo_rpm": int(req_metrics.get("external_calls_by_service_last_minute", {}).get("yahoo", 0)),
                "external_rpm": int(req_metrics.get("external_calls_last_minute", 0)),
                "internal_rpm": int(req_metrics.get("internal_calls_last_minute", 0)),
            },
            "history": history,
            "metrics": req_metrics,
        }
    except Exception as e:
        logger.error(f"Error getting API stats: {e}")
        return {
            "current": {
                "fmp_rpm": 0,
                "yahoo_rpm": 0,
                "external_rpm": 0,
                "internal_rpm": 0,
            },
            "history": [],
            "metrics": {},
        }

@app.post("/api/news/force-refresh")
def api_force_refresh_news():
    """Force immediate news refresh, clearing all caches."""
    try:
        if not _agent_enabled("news_tab"):
            return {"status": "disabled", "ready": False, "initializing": False, "error": None, "disabled": True}

        import json
        import threading
        from api.engine_service import get_engine
        from agents.news_tab_agent import get_news_tab_agent, get_news_tab_agent_if_ready, get_news_tab_agent_status

        engine = get_engine()
        agent = engine.news_agent if engine and hasattr(engine, "news_agent") else None

        # Get active tickers from watchlist
        tickers = []
        try:
            from core.config import WATCHLIST_PATH
            with open(WATCHLIST_PATH, 'r', encoding='utf-8') as f:
                watchlist = json.load(f)
                stocks = watchlist.get("yahoo", watchlist.get("stocks", []))
                crypto = watchlist.get("binance", watchlist.get("crypto", []))
                tickers = (stocks + crypto)[:30]
        except Exception:
            pass

        def _refresh_worker():
            try:
                local_agent = agent or get_news_tab_agent()
                local_agent.clear_cache()
                if tickers:
                    local_agent.refresh_all_news(tickers)
            except Exception as e:
                logger.error(f"Force news refresh failed: {e}")

        threading.Thread(target=_refresh_worker, daemon=True, name="NewsForceRefresh").start()

        status = get_news_tab_agent_status()
        ready = get_news_tab_agent_if_ready() is not None
        return {
            "status": "started",
            "ready": ready,
            "initializing": bool(status.get("initializing")),
            "error": status.get("error"),
            "disabled": False,
        }
    except Exception as e:
        logger.error(f"Error forcing news refresh: {e}", exc_info=True)
        raise _safe_error()


# ==================== Missing Endpoints ====================

@app.get("/api/calendar/state")
def api_calendar_state():
    """Get comprehensive economic calendar state"""
    try:
        def _build():
            from agents.economic_calendar_agent import get_economic_calendar_agent
            agent = get_economic_calendar_agent()
            state = agent.get_state()
            state["disabled"] = not _agent_enabled("economic_calendar")
            return state
        return calendar_state_flight.do("calendar_state", _build)
    except Exception as e:
        logger.error(f"Error getting calendar state: {e}", exc_info=True)
        raise _safe_error()

@app.get("/api/traderadar/state")
def api_traderadar_state(limit: int = 50):
    """Fast read of prepared Trade Radar state."""
    try:
        cache_limit = max(1, min(200, int(limit or 50)))
        key = f"traderadar_state:{cache_limit}"
        cached = _endpoint_payload_get(key, ttl_seconds=20.0)
        if cached is not None:
            cache_manager.record_manual_decision(
                key=f"traderadar-state:{cache_limit}",
                decision="HIT",
                reason="endpoint_payload_cache",
                source="traderadar_state_endpoint",
                ttl_seconds=10,
                age_seconds=None,
            )
            return Response(content=cached, media_type="application/json")
        if not _agent_enabled("trade_radar"):
            data = {
                "as_of": datetime.now(timezone.utc).isoformat(),
                "config": {},
                "suppressed": {},
                "total_candidates": 0,
                "candidates": [],
                "disabled": True,
            }
            payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            _endpoint_payload_set(key, payload)
            return Response(content=payload, media_type="application/json")
        try:
            from agents.trade_radar_agent import get_trade_radar_agent_if_ready

            agent = get_trade_radar_agent_if_ready()
            if agent is not None:
                # UI serve path: suppressed symbols leave the list (Suppress
                # used to store the symbol but the card stayed on screen).
                data = agent.get_state(limit=limit, include_suppressed=False)
                data["disabled"] = False
                payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
                _endpoint_payload_set(key, payload)
                cache_manager.record_manual_decision(
                    key=f"traderadar-state:{cache_limit}",
                    decision="HIT",
                    reason="served from runtime agent cache",
                    source="traderadar_state_endpoint",
                    ttl_seconds=15,
                    age_seconds=None,
                )
                return Response(content=payload, media_type="application/json")
        except Exception:
            pass
        data = get_traderadar_state(limit=limit)
        try:
            from agents.trade_radar_agent import filter_suppressed_candidates

            filter_suppressed_candidates(data)
        except Exception:
            pass
        data["disabled"] = False
        payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        _endpoint_payload_set(key, payload)
        cache_manager.record_manual_decision(
            key=f"traderadar-state:{cache_limit}",
            decision="HIT",
            reason="served from persisted state cache",
            source="traderadar_state_endpoint",
            ttl_seconds=30,
            age_seconds=None,
        )
        return Response(content=payload, media_type="application/json")
    except Exception as e:
        logger.error(f"Error getting traderadar state: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/traderadar/refresh")
def api_traderadar_refresh(request: TradeRadarRefreshRequest):
    """Trigger Trade Radar refresh in background."""
    try:
        if not _agent_enabled("trade_radar"):
            return {"status": "disabled", "request": {"status": "disabled"}}
        from agents.trade_radar_agent import get_trade_radar_agent

        agent = get_trade_radar_agent()
        result = agent.request_refresh(
            force=bool(request.force),
            symbols=request.symbols or [],
        )
        _endpoint_payload_clear_prefix("traderadar_state:")
        return {"status": "accepted", "request": result}
    except Exception as e:
        logger.error(f"Error refreshing traderadar: {e}", exc_info=True)
        raise _safe_error()


@app.put("/api/traderadar/config")
def api_traderadar_config(request: TradeRadarConfigRequest):
    """Update Trade Radar runtime config."""
    try:
        if not _agent_enabled("trade_radar"):
            return {"status": "disabled", "config": {}}
        from agents.trade_radar_agent import get_trade_radar_agent

        updates = {
            "threshold_pct": request.threshold_pct,
            "radar_interval_sec": request.radar_interval_sec,
            "cooldown_sec": request.cooldown_sec,
            "max_candidates": request.max_candidates,
            "max_per_cycle": request.max_per_cycle,
            "backtest_days": request.backtest_days,
            "enable_backtest": request.enable_backtest,
            "ohlc_ttl_sec": request.ohlc_ttl_sec,
            "news_ttl_sec": request.news_ttl_sec,
            "calendar_ttl_sec": request.calendar_ttl_sec,
            "backtest_ttl_sec": request.backtest_ttl_sec,
            "local_only_mode": request.local_only_mode,
            "allow_remote_on_miss": request.allow_remote_on_miss,
            "allow_analysis_remote": request.allow_analysis_remote,
            "allow_ohlc_remote": request.allow_ohlc_remote,
            "allow_news_refresh": request.allow_news_refresh,
            "allow_calendar_refresh": request.allow_calendar_refresh,
        }
        updates = {k: v for k, v in updates.items() if v is not None}

        agent = get_trade_radar_agent()
        config = agent.update_config(updates)
        _endpoint_payload_clear_prefix("traderadar_state:")
        return {"status": "ok", "config": config}
    except Exception as e:
        logger.error(f"Error updating traderadar config: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/traderadar/suppress")
def api_traderadar_suppress(request: TradeRadarSuppressRequest):
    """Suppress symbols from Trade Radar processing for a duration."""
    try:
        if not _agent_enabled("trade_radar"):
            return {"status": "disabled", "suppressed": {}, "count": 0}
        from agents.trade_radar_agent import get_trade_radar_agent

        agent = get_trade_radar_agent()
        result = agent.suppress_symbols(
            symbols=request.symbols or [],
            duration_sec=request.duration_sec,
        )
        _endpoint_payload_clear_prefix("traderadar_state:")
        return {"status": "ok", **result}
    except Exception as e:
        logger.error(f"Error suppressing traderadar symbols: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/traderadar/unsuppress")
def api_traderadar_unsuppress(request: TradeRadarSuppressRequest):
    """Lift a Trade Radar suppression early (undo for the Suppress button)."""
    try:
        if not _agent_enabled("trade_radar"):
            return {"status": "disabled", "suppressed": {}, "count": 0}
        from agents.trade_radar_agent import get_trade_radar_agent

        agent = get_trade_radar_agent()
        result = agent.unsuppress_symbols(symbols=request.symbols or [])
        _endpoint_payload_clear_prefix("traderadar_state:")
        return {"status": "ok", **result}
    except Exception as e:
        logger.error(f"Error un-suppressing traderadar symbols: {e}", exc_info=True)
        raise _safe_error()


# ── Free order-flow evidence (whale / institutional proxy) ─────────────────
@app.get("/api/flow/whale")
def api_flow_whale(symbol: str, force: bool = False):
    """Crypto whale-flow evidence from public Binance/Bybit/OKX books + tape."""
    try:
        from core.flow_indicators import configure_from_shared, get_whale_flow
        configure_from_shared(_shared_config_for_flow())
        return get_whale_flow(symbol, force=bool(force))
    except Exception as e:
        logger.error(f"Whale flow endpoint failed: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/flow/institutional")
def api_flow_institutional(symbol: str, force: bool = False):
    """Stock INSTITUTIONAL FLOW PROXY (free data; honestly labelled, not full L2)."""
    try:
        from core.flow_indicators import configure_from_shared, get_institutional_flow
        configure_from_shared(_shared_config_for_flow())
        return get_institutional_flow(symbol, force=bool(force))
    except Exception as e:
        logger.error(f"Institutional flow endpoint failed: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/flow/status")
def api_flow_status():
    """Flow-indicator source health: cache ages, config, last biases."""
    try:
        from core.flow_indicators import configure_from_shared, flow_evidence_status
        configure_from_shared(_shared_config_for_flow())
        return flow_evidence_status()
    except Exception as e:
        logger.error(f"Flow status endpoint failed: {e}", exc_info=True)
        raise _safe_error()


def _shared_config_for_flow():
    try:
        from api.state_service import get_shared_config
        return get_shared_config()
    except Exception:
        return None


_PREMARKET_BUILD_LOCK = threading.Lock()
_PREMARKET_BUILD_INFLIGHT: Optional[Dict[str, Any]] = None


def _premarket_state_build(build, timeout: float):
    """Run `build()` for /api/premarket on at most ONE background thread.

    The endpoint used to create a private ThreadPoolExecutor per request and,
    on its 10s timeout, abandon it with the worker still running — one leaked
    thread per timed-out request for as long as the agent stayed blocked. Here
    a request joins the build already in flight; a new thread starts only once
    the previous build has finished. Returns (finished, result); re-raises the
    build's own exception."""
    global _PREMARKET_BUILD_INFLIGHT
    with _PREMARKET_BUILD_LOCK:
        job = _PREMARKET_BUILD_INFLIGHT
        if job is None or job["done"].is_set():
            job = {"done": threading.Event(), "result": None, "error": None}

            def _run(job=job):
                try:
                    job["result"] = build()
                except BaseException as exc:  # surfaced to the waiting request
                    job["error"] = exc
                finally:
                    job["done"].set()

            _PREMARKET_BUILD_INFLIGHT = job
            threading.Thread(target=_run, daemon=True, name="premarket-state-build").start()
    if not job["done"].wait(timeout=timeout):
        return False, None
    if job["error"] is not None:
        raise job["error"]
    return True, job["result"]


@app.get("/api/premarket")
def api_premarket_state():
    """Return Premarket Scanner cached output only (memory -> file fallback)."""
    try:
        cached = _endpoint_payload_get("premarket_state", ttl_seconds=30.0)
        if cached is not None:
            # Cached bytes are already normalized by _json_payload_bytes at
            # set time — re-normalizing parsed+dumped ~1MB JSON on every hit
            # and blew the endpoint's latency budget under load.
            cache_manager.record_manual_decision(
                key="premarket-state",
                decision="HIT",
                reason="endpoint_payload_cache",
                source="premarket_state_endpoint",
                ttl_seconds=30,
                age_seconds=None,
            )
            return Response(content=cached, media_type="application/json")

        ctx = get_request_context()
        forced_cache = bool(ctx and ctx.cache_only)
        if forced_cache and not IS_TEST_MODE:
            disk_state = _load_json_file_payload(_PREMARKET_DISK_STATE_PATH)
            if isinstance(disk_state, dict):
                disk_state["disabled"] = bool(disk_state.get("disabled", False) or (not _agent_enabled("premarket_scanner")))
                payload = _json_payload_bytes(disk_state)
                _endpoint_payload_set("premarket_state", payload)
                _set_ctx_cache_reason("HIT")
                cache_manager.record_manual_decision(
                    key="premarket-state",
                    decision="HIT",
                    reason="disk_fallback_cache_only",
                    source="premarket_state_endpoint",
                    ttl_seconds=30,
                    age_seconds=None,
                )
                return Response(content=payload, media_type="application/json")
            _set_ctx_cache_reason("MISS")
            return {
                "candidates": [],
                "refreshing": False,
                "status": {"state": "CACHE_ONLY_MISS"},
                "disabled": not _agent_enabled("premarket_scanner"),
            }

        from agents.premarket_scanner_agent import get_premarket_scanner_agent

        def _build_premarket():
            agent = get_premarket_scanner_agent()
            d = agent.get_state()
            d["disabled"] = not _agent_enabled("premarket_scanner")
            return agent, d

        # One shared build thread (see _premarket_state_build): a request that
        # times out joins the build still in flight on the next call instead
        # of leaving a private worker thread behind each time.
        finished, built = _premarket_state_build(_build_premarket, 10.0)
        if finished:
            agent, data = built
        else:
            logger.warning("Premarket state build timed out (10s), returning empty state")
            data = {"candidates": [], "refreshing": True, "status": {"state": "WARMING_UP"}, "disabled": not _agent_enabled("premarket_scanner")}
            agent = None
        # Self-heal: if cache is empty or stale while agent is enabled, ensure agent loop is running and request refresh.
        try:
            if (not forced_cache) and _agent_enabled("premarket_scanner"):
                candidates_empty = not (data.get("candidates") or [])
                status_obj = data.get("status") or {}
                last_success = status_obj.get("last_success_time")
                data_stale = False
                if last_success:
                    try:
                        from datetime import datetime, timezone
                        last_dt = datetime.fromisoformat(str(last_success).replace("Z", "+00:00"))
                        age_sec = (datetime.now(timezone.utc) - last_dt.astimezone(timezone.utc)).total_seconds()
                        if age_sec > 600:
                            data_stale = True
                    except Exception:
                        data_stale = True
                else:
                    data_stale = True
                if candidates_empty or data_stale:
                    if hasattr(agent, "start"):
                        agent.start()
                    if hasattr(agent, "request_refresh"):
                        # Wake only (force=False): a page load must not
                        # override the closed-market budget guard. Only the
                        # explicit Refresh button forces a rebuild.
                        try:
                            agent.request_refresh(force=False)
                        except TypeError:
                            agent.request_refresh()
        except Exception:
            pass
        payload = _json_payload_bytes(data)
        _set_ctx_cache_reason("MISS")
        _endpoint_payload_set("premarket_state", payload)
        cache_manager.record_manual_decision(
            key="premarket-state",
            decision="HIT",
            reason="served from premarket cached state",
            source="premarket_state_endpoint",
                ttl_seconds=30,
            age_seconds=None,
        )
        return Response(content=payload, media_type="application/json")
    except Exception as e:
        logger.error(f"Error getting premarket state: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/premarket/refresh")
def api_premarket_refresh():
    """Trigger a single Premarket run while keeping cache-first policy."""
    try:
        if not _agent_enabled("premarket_scanner"):
            raise HTTPException(status_code=409, detail="agent disabled")
        from agents.premarket_scanner_agent import get_premarket_scanner_agent

        agent = get_premarket_scanner_agent()
        if hasattr(agent, "is_market_open") and bool(agent.is_market_open()):
            raise HTTPException(status_code=409, detail="market open")
        if hasattr(agent, "request_refresh"):
            if hasattr(agent, "start"):
                try:
                    agent.start()
                except Exception:
                    pass
            result = agent.request_refresh()
            _endpoint_payload_clear("scanner_state")
            _endpoint_payload_clear("premarket_state")
            return {"status": "accepted", "request": result, "async": True}
        result = agent.run_once()
        _endpoint_payload_clear("scanner_state")
        _endpoint_payload_clear("premarket_state")
        return {"status": "ok", "data": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error refreshing premarket scanner: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/pre-earnings")
def api_pre_earnings_state():
    """Return Pre-Earnings cached output only (memory -> file fallback)."""
    try:
        _sync_pre_earnings_cache_revision()
        cached = _endpoint_payload_get("pre_earnings_state", ttl_seconds=60.0)
        if cached is not None and _sync_pre_earnings_cache_revision():
            cached = None
        if cached is not None and not IS_TEST_MODE and b'"top_predictions"' not in cached:
            # A request can arrive during the brief startup window before the
            # agent singleton is ready and seed the endpoint cache from a
            # legacy disk snapshot. As soon as the local agent exists, bypass
            # that incomplete entry so normalized rankings appear immediately
            # instead of waiting for the endpoint TTL.
            try:
                from agents.pre_earnings_agent import get_pre_earnings_agent_if_ready

                if get_pre_earnings_agent_if_ready() is not None:
                    cached = None
            except Exception:
                pass
        if cached is not None:
            cache_manager.record_manual_decision(
                key="pre-earnings-state",
                decision="HIT",
                reason="endpoint_payload_cache",
                source="pre_earnings_state_endpoint",
                ttl_seconds=60,
                age_seconds=None,
            )
            # Fast path: cached bytes are normalized by _json_payload_bytes at
            # set time — re-normalizing parsed+dumped ~1MB JSON on every hit
            # blew the endpoint's latency budget under load. But a corrupt or
            # legacy cache entry may still carry a raw NaN/Infinity literal
            # (invalid JSON per RFC 8259). Sanitize ONLY then, via the existing
            # _normalize_json_payload_bytes helper (NaN/Inf -> null), so the
            # zero-cost passthrough holds for the common clean-cache case.
            if b"NaN" in cached or b"Infinity" in cached or b"-Infinity" in cached:
                cached = _normalize_json_payload_bytes(cached)
            return Response(
                content=cached,
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )

        ctx = get_request_context()
        forced_cache = bool(ctx and ctx.cache_only)
        if forced_cache and not IS_TEST_MODE:
            from agents.pre_earnings_agent import (
                PRE_EARNINGS_OUTPUT_PATH,
                get_pre_earnings_agent_if_ready,
                guard_pre_earnings_recommendation_freshness,
            )

            # UI reads are cache-only, but an already initialized agent is also
            # a local cache. Prefer its normalized in-memory state so additive
            # fields (daily ranking, legacy migrations, fresh concurrent symbol
            # updates) are visible immediately after a restart without making
            # any provider calls. Never initialize the agent from this GET.
            ready_agent = get_pre_earnings_agent_if_ready()
            if ready_agent is not None:
                try:
                    memory_state = ready_agent.get_state()
                except Exception as exc:
                    logger.warning("Pre-earnings memory cache read failed; using disk fallback: %s", exc)
                else:
                    if isinstance(memory_state, dict):
                        memory_state = _prefer_newer_pre_earnings_snapshot(memory_state)
                        memory_state["disabled"] = bool(
                            memory_state.get("disabled", False)
                            or (not _agent_enabled("pre_earnings", default=False))
                        )
                        payload = _json_payload_bytes(memory_state)
                        _endpoint_payload_set("pre_earnings_state", payload)
                        _set_ctx_cache_reason("HIT")
                        cache_manager.record_manual_decision(
                            key="pre-earnings-state",
                            decision="HIT",
                            reason="memory_cache_only",
                            source="pre_earnings_state_endpoint",
                            ttl_seconds=60,
                            age_seconds=None,
                        )
                        return Response(
                            content=payload,
                            media_type="application/json",
                            headers={"Cache-Control": "no-store"},
                        )

            disk_state = _load_json_file_payload(PRE_EARNINGS_OUTPUT_PATH)
            if isinstance(disk_state, dict):
                disk_state.setdefault("state_revision", 0)
                guard_pre_earnings_recommendation_freshness(disk_state)
                disk_state["disabled"] = bool(
                    disk_state.get("disabled", False) or (not _agent_enabled("pre_earnings", default=False))
                )
                payload = _json_payload_bytes(disk_state)
                _endpoint_payload_set("pre_earnings_state", payload)
                _set_ctx_cache_reason("HIT")
                cache_manager.record_manual_decision(
                    key="pre-earnings-state",
                    decision="HIT",
                    reason="disk_fallback_cache_only",
                    source="pre_earnings_state_endpoint",
                    ttl_seconds=60,
                    age_seconds=None,
                )
                return Response(
                    content=payload,
                    media_type="application/json",
                    headers={"Cache-Control": "no-store"},
                )
            _set_ctx_cache_reason("MISS")
            return JSONResponse(
                content={
                    "state_revision": 0,
                    "items": [],
                    "refreshing": False,
                    "status": {"state": "CACHE_ONLY_MISS"},
                    "disabled": not _agent_enabled("pre_earnings", default=False),
                },
                headers={"Cache-Control": "no-store"},
            )

        import concurrent.futures
        from agents.pre_earnings_agent import get_pre_earnings_agent

        def _build_pre_earnings():
            agent = get_pre_earnings_agent()
            d = _prefer_newer_pre_earnings_snapshot(agent.get_state())
            d["disabled"] = not _agent_enabled("pre_earnings", default=False)
            return agent, d

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(_build_pre_earnings)
            agent, data = future.result(timeout=10.0)
        except concurrent.futures.TimeoutError:
            logger.warning("Pre-earnings state build timed out (10s), returning empty state")
            data = {"state_revision": 0, "items": [], "refreshing": True, "status": {"state": "WARMING_UP"}, "disabled": not _agent_enabled("pre_earnings", default=False)}
            agent = None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        # If state is stale after restart, nudge an async refresh so UI repopulates quickly.
        if (not forced_cache) and _agent_enabled("pre_earnings", default=False) and hasattr(agent, "request_refresh"):
            stale = False
            try:
                status = dict(data.get("status", {}) or {})
                last_ts = status.get("last_success_time") or status.get("last_run_time")
                if not last_ts:
                    stale = True
                else:
                    dt = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    refresh_sec = max(30, int(data.get("refresh_seconds") or 600))
                    stale = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() > refresh_sec
            except Exception:
                stale = True
            if stale or not list(data.get("items") or []):
                try:
                    agent.request_refresh()
                except Exception:
                    pass

        payload = _json_payload_bytes(data)
        _set_ctx_cache_reason("MISS")
        _endpoint_payload_set("pre_earnings_state", payload)
        cache_manager.record_manual_decision(
            key="pre-earnings-state",
            decision="HIT",
            reason="served from pre-earnings cached state",
            source="pre_earnings_state_endpoint",
                ttl_seconds=60,
            age_seconds=None,
        )
        return Response(
            content=payload,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        logger.error(f"Error getting pre-earnings state: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/pre-earnings/refresh")
def api_pre_earnings_refresh():
    """Trigger a single Pre-Earnings run while keeping cache-first policy."""
    try:
        if not _agent_enabled("pre_earnings", default=False):
            raise HTTPException(status_code=409, detail="agent disabled")
        from agents.pre_earnings_agent import get_pre_earnings_agent

        agent = get_pre_earnings_agent()
        if hasattr(agent, "request_refresh"):
            if hasattr(agent, "start"):
                try:
                    agent.start()
                except Exception:
                    pass
            result = agent.request_refresh()
            coverage = None
            if hasattr(agent, "request_coverage_warmup"):
                coverage = agent.request_coverage_warmup(
                    symbols=[],
                    batch_size=5,
                    only_needs_coverage=True,
                    allow_remote_on_miss=True,
                )
            _invalidate_pre_earnings_state_cache(
                warmup_status=_live_pre_earnings_warmup_status(agent),
            )
            return {
                "status": "accepted",
                "request": result,
                "coverage": coverage,
                "async": True,
            }
        result = agent.run_once()
        _invalidate_pre_earnings_state_cache(
            warmup_status=_live_pre_earnings_warmup_status(agent),
        )
        return {"status": "ok", "data": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error refreshing pre-earnings scanner: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/pre-earnings/refresh-status")
def api_pre_earnings_refresh_status():
    """Lightweight polling contract; never serializes the multi-megabyte item list."""
    try:
        from agents.pre_earnings_agent import get_pre_earnings_agent

        agent = get_pre_earnings_agent()
        if hasattr(agent, "get_refresh_status"):
            payload = agent.get_refresh_status()
        else:
            runtime = agent.get_runtime_status() if hasattr(agent, "get_runtime_status") else {}
            payload = {
                "state_revision": 0,
                "status": {
                    "last_run_time": runtime.get("last_run"),
                    "last_success_time": runtime.get("last_success"),
                    "last_error": runtime.get("last_error"),
                    "next_run_in": runtime.get("next_run_in"),
                },
                "disabled": not _agent_enabled("pre_earnings", default=False),
            }
        status = dict(payload.get("status", {}) or {})
        status["coverage_warmup"] = _live_pre_earnings_warmup_status(agent)
        payload["status"] = status
        return JSONResponse(
            content=payload,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        logger.error(f"Error getting pre-earnings refresh status: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/pre-earnings/refresh-symbol")
def api_pre_earnings_refresh_symbol(symbol: str):
    """Refresh calendar plus missing analysis inputs for one symbol."""
    try:
        if not _agent_enabled("pre_earnings", default=False):
            raise HTTPException(status_code=409, detail="agent disabled")
        from agents.pre_earnings_agent import get_pre_earnings_agent

        normalized = _normalize_pre_earnings_symbol(symbol)

        agent = get_pre_earnings_agent()
        if hasattr(agent, "start"):
            try:
                agent.start()
            except Exception:
                logger.debug("Pre-Earnings symbol refresh could not start agent thread", exc_info=True)
        if hasattr(agent, "refresh_symbol_analysis"):
            result = agent.refresh_symbol_analysis(normalized)
        elif hasattr(agent, "refresh_symbol_calendar"):
            result = agent.refresh_symbol_calendar(normalized)
        else:
            result = {"status": "unsupported", "symbol": normalized}
        _invalidate_pre_earnings_state_cache(
            warmup_status=_live_pre_earnings_warmup_status(agent),
        )
        return {"status": "ok", "result": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error refreshing pre-earnings symbol: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/pre-earnings/refresh-ai")
def api_pre_earnings_refresh_ai(symbol: str):
    """Force a fresh AI verdict for one Pre-Earnings ticker (UI 'Refresh AI')."""
    try:
        if not _agent_enabled("pre_earnings", default=False):
            raise HTTPException(status_code=409, detail="agent disabled")
        from agents.pre_earnings_agent import get_pre_earnings_agent

        normalized = _normalize_pre_earnings_symbol(symbol)

        agent = get_pre_earnings_agent()
        if hasattr(agent, "force_ai_for_symbol"):
            result = agent.force_ai_for_symbol(normalized)
        else:
            result = {"status": "unsupported", "symbol": normalized}

        # Only bust the GET cache when state actually changed (success).
        if isinstance(result, dict) and result.get("status") == "ok":
            _invalidate_pre_earnings_state_cache()
        return {"status": result.get("status"), "result": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error forcing pre-earnings AI for symbol: {e}", exc_info=True)
        raise _safe_error()


@app.post("/api/pre-earnings/coverage-warmup", include_in_schema=False)
@app.post("/api/pre-earnings/coverage/warmup")
def api_pre_earnings_coverage_warmup(request: PreEarningsCoverageWarmupRequest):
    """Warm caches for low-coverage pre-earnings symbols in small non-blocking batches."""
    try:
        if not _agent_enabled("pre_earnings", default=False):
            raise HTTPException(status_code=409, detail="agent disabled")
        from agents.pre_earnings_agent import get_pre_earnings_agent

        symbols = _normalize_pre_earnings_coverage_symbols(request.symbols)
        agent = get_pre_earnings_agent()
        if hasattr(agent, "start"):
            try:
                agent.start()
            except Exception:
                pass

        batch_size = max(1, min(5, int(request.batch_size or 5)))
        result = agent.request_coverage_warmup(
            symbols=symbols,
            batch_size=batch_size,
            only_needs_coverage=bool(request.only_needs_coverage if request.only_needs_coverage is not None else True),
            allow_remote_on_miss=bool(request.allow_remote_on_miss if request.allow_remote_on_miss is not None else True),
        )
        _invalidate_pre_earnings_state_cache(
            warmup_status=_live_pre_earnings_warmup_status(agent),
        )
        return {"status": result.get("status", "accepted"), "request": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting pre-earnings coverage warmup: {e}", exc_info=True)
        raise _safe_error()


@app.get("/api/pre-earnings/coverage/warmup")
def api_pre_earnings_coverage_warmup_status(response: Response):
    """Return progress for ongoing pre-earnings coverage warmup."""
    try:
        from agents.pre_earnings_agent import get_pre_earnings_agent

        agent = get_pre_earnings_agent()
        status = (
            agent.get_coverage_warmup_status()
            if hasattr(agent, "get_coverage_warmup_status")
            else {}
        )
        _sync_pre_earnings_cache_revision(warmup_status=status)
        response.headers["Cache-Control"] = "no-store"
        return {"status": "ok", "coverage_warmup": status}
    except Exception as e:
        logger.error(f"Error reading pre-earnings coverage warmup status: {e}", exc_info=True)
        raise _safe_error()


# ---------------------------------------------------------------------------
# /api/pre-earnings/dossier — per-row verification dossier (I04, 2026-09-07).
# On-demand for the selected Pre-Earnings row: opposing evidence, internal
# contradictions, data gaps, failed gates, and the frozen first/shortlist
# evidence summaries from the open-prediction registry. Read-only: never
# initializes the agent and never mutates agent state.
# ---------------------------------------------------------------------------
@app.get("/api/pre-earnings/dossier")
def api_pre_earnings_dossier(symbol: str = "", event_utc: str = ""):
    try:
        from agents.pre_earnings_agent import get_pre_earnings_agent_if_ready

        agent = get_pre_earnings_agent_if_ready()
        if agent is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "Pre-Earnings agent not ready"},
            )
        dossier = agent.get_verification_dossier(symbol, event_utc=event_utc or None)
        if dossier is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"Symbol {str(symbol or '').upper().strip() or '?'} not in the Pre-Earnings slate"},
            )
        return JSONResponse(
            content=dossier,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        if isinstance(e, ValueError):
            return JSONResponse(status_code=400, content={"detail": "Invalid earnings event timestamp"})
        if isinstance(e, TimeoutError):
            return JSONResponse(status_code=503, content={"detail": "Pre-Earnings state is busy; retry shortly"}, headers={"Retry-After": "1", "Cache-Control": "no-store"})
        logger.error(f"Error reading pre-earnings dossier for {symbol}: {e}", exc_info=True)
        raise _safe_error()


# ---------------------------------------------------------------------------
# /api/pre-earnings/track-record — History / Track-Record panel (Task #11)
# Returns: recent settled outcomes, per-verdict + per-confidence-band accuracy
# metrics with concrete numbers, and the aggregate AI summary + future
# recommendations (separate prompt, cached 300s). Cache-first (60s) like the
# main pre-earnings endpoint; NaN/Inf sanitized to null on serve.
# ---------------------------------------------------------------------------
@app.get("/api/pre-earnings/track-record")
def api_pre_earnings_track_record():
    try:
        cached = _endpoint_payload_get("pre_earnings_track_record", ttl_seconds=60.0)
        if cached is not None:
            if b"NaN" in cached or b"Infinity" in cached or b"-Infinity" in cached:
                cached = _normalize_json_payload_bytes(cached)
            return Response(content=cached, media_type="application/json")

        from agents.pre_earnings_agent import (
            direction_label_from_score,
            get_pre_earnings_agent,
        )
        # Same plausibility bound as the metrics and the Evidence Lab: a move
        # beyond it is a price-data error until verified, so the row is shown
        # but not scored as a hit or a miss.
        from core.pre_earnings_learning import MAX_PLAUSIBLE_MOVE_PCT as _pe_max_plausible_move

        agent = get_pre_earnings_agent()
        cfg = agent._get_config() if hasattr(agent, "_get_config") else {}

        # Bounded one-time retro repair (re-audit P1): outcomes settled before
        # AI provenance was persisted never received their registry verdict,
        # so ai_coverage read 0 for weeks despite the verdicts existing in the
        # durable per-event AI registry. Runs before metrics/rows so both see
        # repaired data; persists only when something actually matched (no-op
        # afterwards) and never blocks the endpoint on failure.
        if hasattr(agent, "_retro_backfill_outcome_ai_provenance"):
            try:
                agent._retro_backfill_outcome_ai_provenance()
            except Exception:
                logger.warning("Pre-earnings retro AI-provenance backfill failed (non-fatal)", exc_info=True)

        if hasattr(agent, "get_feedback_snapshot"):
            feedback_snapshot = agent.get_feedback_snapshot(deduplicate_outcomes=True)
        else:
            feedback_snapshot = copy.deepcopy(agent._feedback) if hasattr(agent, "_feedback") else {}
        if hasattr(agent, "_compute_track_record_metrics"):
            metrics = (
                agent._compute_track_record_metrics(
                    cfg,
                    outcomes=list(feedback_snapshot.get("outcomes", []) or []),
                )
                if hasattr(agent, "get_feedback_snapshot")
                else agent._compute_track_record_metrics(cfg)
            )
        else:
            metrics = {}

        # AI summary + future recommendations (separate prompt, 300s cache).
        # Bounded so a cold cache never blocks the endpoint beyond a few seconds.
        ai_summary = None
        if hasattr(agent, "get_track_record_ai_summary"):
            import concurrent.futures

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                future = executor.submit(agent.get_track_record_ai_summary, cfg)
                ai_summary = future.result(timeout=12.0)
            except concurrent.futures.TimeoutError:
                ai_summary = None
            except Exception:
                ai_summary = None
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

        # Recent settled outcomes (newest first), capped to keep the payload small.
        recent_n = max(20, min(500, int(cfg.get("track_record_recent_rows", 200))))
        raw_outcomes = list(feedback_snapshot.get("outcomes", []) or [])

        def _parse_iso_local(v):
            try:
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                return None

        raw_outcomes.sort(
            key=lambda r: _parse_iso_local(r.get("event_utc") or r.get("evaluation_utc"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        outcomes_recent = []
        for o in raw_outcomes[:recent_n]:
            if not isinstance(o, dict):
                continue
            try:
                bias = int(o.get("predicted_direction_bias") or 0)
            except (TypeError, ValueError):
                bias = 0
            s = o.get("realized_signed_move_pct")
            try:
                s_val = float(s) if s is not None else None
            except (TypeError, ValueError):
                s_val = None
            if s_val is not None and not math.isfinite(s_val):
                s_val = None
            move_implausible = bool(s_val is not None and abs(s_val) > _pe_max_plausible_move)
            hit = None
            if s_val is not None and not move_implausible and abs(bias) >= 15:
                hit = (bias > 0 and s_val > 0) or (bias < 0 and s_val < 0)
            outcomes_recent.append({
                "symbol": o.get("symbol"),
                "event_utc": o.get("event_utc"),
                "evaluation_utc": o.get("evaluation_utc"),
                "session": o.get("session"),
                "sector": o.get("sector"),
                # Peer read-through grouping fields (owner requirement,
                # 2026-11): the frontend groups history by sector/theme and by
                # day client-side. peer_key_label is null for legacy rows
                # settled before peer keys existed (honest pass-through);
                # event_day is the UTC calendar day of the earnings event.
                "peer_key_label": o.get("peer_key_label"),
                "event_day": (str(o.get("event_utc") or "")[:10] or None),
                "cap_bucket": o.get("cap_bucket"),
                "predicted_direction_bias": bias,
                # Explicit per-row label under the app's ±15 directional
                # threshold (the endpoint already advertises
                # directional_threshold=15) so the frontend stops deriving
                # LONG/SHORT from bias>0 — AAPL +2 rendered LONG and PRM -5
                # rendered SHORT before this (audit P0-3).
                "label": direction_label_from_score(bias),
                "predicted_ai_verdict": o.get("predicted_ai_verdict"),
                "predicted_ai_confidence": o.get("predicted_ai_confidence"),
                "predicted_ai_catalyst": o.get("predicted_ai_catalyst"),
                # AI-verdict provenance per row (re-audit P1): distinguishes a
                # verdict registered at prediction time from one backfilled
                # from the durable per-event registry ("ai_cache_backfill");
                # null when the outcome never had a provable AI verdict.
                "predicted_ai_provenance": o.get("predicted_ai_provenance"),
                "predicted_gap_risk": o.get("predicted_gap_risk"),
                "predicted_trader_score": o.get("predicted_trader_score"),
                "top_prediction_rank": o.get("top_prediction_rank"),
                "was_top_prediction": bool(o.get("was_top_prediction")),
                "realized_signed_move_pct": (round(s_val, 4) if s_val is not None else None),
                "move_implausible": move_implausible,
                "realized_high_gap": o.get("realized_high_gap"),
                "hit": hit,
            })

        # Dedicated Top-10 audit trail. These rows are intentionally filtered
        # from the complete settled-outcome registry rather than inferred from
        # the general History table: only recommendations actually displayed in
        # Top 10 belong in this population.
        raw_top_history = [
            o for o in raw_outcomes
            if isinstance(o, dict) and bool(o.get("was_top_prediction"))
        ]

        def _top_history_sort_key(row):
            day_str = str(row.get("event_utc") or "")[:10]
            try:
                score = int(
                    row.get("top_prediction_trader_score")
                    if row.get("top_prediction_trader_score") is not None
                    else (row.get("predicted_trader_score") or 0)
                )
            except (TypeError, ValueError):
                score = 0
            return (day_str, score)

        # Trader-facing order: newest earnings day first, then highest Trader Score to lowest (de la mare la mic)
        raw_top_history.sort(key=_top_history_sort_key, reverse=True)

        top_predictions_history = []
        current_day = None
        day_rank = 0
        for o in raw_top_history:
            if not isinstance(o, dict):
                continue
            event_day_str = (str(o.get("event_utc") or "")[:10] or None)
            if event_day_str != current_day:
                current_day = event_day_str
                day_rank = 1
            else:
                day_rank += 1

            try:
                top_bias = int(
                    o.get("top_prediction_bias")
                    if o.get("top_prediction_bias") is not None
                    else (o.get("predicted_direction_bias") or 0)
                )
            except (TypeError, ValueError):
                top_bias = 0
            top_side = str(o.get("top_prediction_direction") or "").upper().strip()
            if top_side not in {"LONG", "SHORT"}:
                top_side = direction_label_from_score(top_bias)
            try:
                top_move = float(o.get("realized_signed_move_pct"))
                if not math.isfinite(top_move):
                    top_move = None
            except (TypeError, ValueError):
                top_move = None
            top_move_implausible = bool(top_move is not None and abs(top_move) > _pe_max_plausible_move)
            top_hit = None
            if (
                top_move is not None
                and top_move != 0.0
                and not top_move_implausible
                and top_side in {"LONG", "SHORT"}
            ):
                top_hit = (top_side == "LONG" and top_move > 0) or (top_side == "SHORT" and top_move < 0)
            best_rank = o.get("top_prediction_best_rank") or o.get("top_prediction_rank")
            top_predictions_history.append({
                "symbol": o.get("symbol"),
                "event_utc": o.get("event_utc"),
                "event_day": event_day_str,
                "evaluation_utc": o.get("evaluation_utc"),
                "session": o.get("session"),
                "sector": o.get("sector"),
                "direction": top_side if top_side in {"LONG", "SHORT"} else None,
                "direction_bias": top_bias,
                "rank": best_rank or day_rank,
                "entry_rank": o.get("top_prediction_entry_rank") or o.get("top_prediction_rank"),
                "best_rank": best_rank or day_rank,
                "day_rank": day_rank,
                "trader_score": (
                    o.get("top_prediction_trader_score")
                    if o.get("top_prediction_trader_score") is not None
                    else o.get("predicted_trader_score")
                ),
                "score_band": o.get("top_prediction_score_band") or o.get("predicted_trader_score_band"),
                "first_seen_utc": o.get("top_prediction_first_seen_utc"),
                "snapshot_quality": o.get("top_prediction_snapshot_quality") or "legacy_reconstructed",
                "realized_signed_move_pct": round(top_move, 4) if top_move is not None else None,
                "move_implausible": top_move_implausible,
                "hit": top_hit,
                "status": "SETTLED",
            })
            if len(top_predictions_history) >= recent_n:
                break

        feedback_store = feedback_snapshot if isinstance(feedback_snapshot, dict) else {}
        raw_open_predictions = list((feedback_store.get("open_predictions", {}) or {}).values())
        raw_top_pending = [
            row for row in raw_open_predictions
            if isinstance(row, dict) and bool(row.get("was_top_prediction"))
        ]
        def _pending_top_sort_key(row):
            day_str = str(row.get("event_utc") or "")[:10]
            try:
                score = int(
                    row.get("top_prediction_trader_score")
                    if row.get("top_prediction_trader_score") is not None
                    else (row.get("predicted_trader_score") or 0)
                )
            except (TypeError, ValueError):
                score = 0
            return (day_str, score)

        # Trader-facing order: newest earnings first, then highest Trader Score to lowest (de la mare la mic)
        raw_top_pending.sort(key=_pending_top_sort_key, reverse=True)
        now_utc = datetime.now(timezone.utc)
        top_predictions_pending = []
        pending_current_day = None
        pending_day_rank = 0
        for p in raw_top_pending[:recent_n]:
            event_day_str = (str(p.get("event_utc") or "")[:10] or None)
            if event_day_str != pending_current_day:
                pending_current_day = event_day_str
                pending_day_rank = 1
            else:
                pending_day_rank += 1

            try:
                pending_bias = int(
                    p.get("top_prediction_bias")
                    if p.get("top_prediction_bias") is not None
                    else (p.get("predicted_direction_bias") or 0)
                )
            except (TypeError, ValueError):
                pending_bias = 0
            pending_side = str(p.get("top_prediction_direction") or "").upper().strip()
            if pending_side not in {"LONG", "SHORT"}:
                pending_side = direction_label_from_score(pending_bias)
            event_dt = _parse_iso_local(p.get("event_utc"))
            p_best_rank = p.get("top_prediction_best_rank") or p.get("top_prediction_rank")
            top_predictions_pending.append({
                "symbol": p.get("symbol"),
                "event_utc": p.get("event_utc"),
                "event_day": event_day_str,
                "session": p.get("session"),
                "sector": p.get("sector"),
                "direction": pending_side if pending_side in {"LONG", "SHORT"} else None,
                "direction_bias": pending_bias,
                "rank": p_best_rank or pending_day_rank,
                "entry_rank": p.get("top_prediction_entry_rank") or p.get("top_prediction_rank"),
                "best_rank": p_best_rank or pending_day_rank,
                "latest_rank": p.get("top_prediction_latest_rank") or p.get("top_prediction_rank"),
                "day_rank": pending_day_rank,
                "trader_score": (
                    p.get("top_prediction_trader_score")
                    if p.get("top_prediction_trader_score") is not None
                    else p.get("predicted_trader_score")
                ),
                "score_band": p.get("top_prediction_score_band") or p.get("predicted_trader_score_band"),
                "first_seen_utc": p.get("top_prediction_first_seen_utc"),
                "snapshot_quality": p.get("top_prediction_snapshot_quality") or "legacy_reconstructed",
                "status": "UPCOMING" if event_dt is not None and event_dt > now_utc else "MATURING",
            })

        top_prediction_metrics = dict(metrics.get("top_predictions", {}) or {})
        top_prediction_metrics["pending"] = len(raw_top_pending)
        top_prediction_metrics.setdefault("settled", top_prediction_metrics.get("total", len(top_predictions_history)))

        # Pulse pick accuracy (mirrors the per-item track record; scored on
        # archived picks after the 36h maturation window). Best-effort: an
        # older agent without the helper yields an empty pulse section.
        pulse_metrics = {}
        pulse_recent: list = []
        if hasattr(agent, "_compute_pulse_track_record_metrics"):
            try:
                pulse_metrics = agent._compute_pulse_track_record_metrics(cfg) or {}
            except Exception:
                pulse_metrics = {}
        if hasattr(agent, "_pulse_store"):
            try:
                pulse_lock = getattr(agent, "_pulse_store_lock", None)
                if pulse_lock is not None:
                    with pulse_lock:
                        pulse_snapshot = copy.deepcopy(agent._pulse_store or {})
                else:
                    pulse_snapshot = copy.deepcopy(agent._pulse_store or {})
                raw_picks = list(pulse_snapshot.get("archive", []) or [])
            except Exception:
                raw_picks = []
            scored = [
                p for p in raw_picks
                if isinstance(p, dict) and p.get("realized_signed_move_pct") is not None
            ]
            scored.sort(
                key=lambda r: _parse_iso_local(r.get("evaluation_utc") or r.get("archived_at"))
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            pulse_recent_n = max(10, min(100, int(cfg.get("pulse_track_record_recent_rows", 50))))
            for p in scored[:pulse_recent_n]:
                s_val = None
                try:
                    s_val = float(p.get("realized_signed_move_pct"))
                    if not math.isfinite(s_val):
                        s_val = None
                except (TypeError, ValueError):
                    s_val = None
                pulse_recent.append({
                    "symbol": p.get("symbol"),
                    "side": p.get("side"),
                    "event_utc": p.get("earnings_time_utc"),
                    "evaluation_utc": p.get("evaluation_utc"),
                    "realized_signed_move_pct": (round(s_val, 4) if s_val is not None else None),
                    "hit": p.get("hit"),
                    "archive_reason": p.get("archive_reason"),
                })

        payload_data = {
            "as_of_utc": datetime.now(timezone.utc).isoformat(),
            "directional_threshold": 15,
            "sample_size": metrics.get("sample_size", 0),
            "ai_coverage": metrics.get("ai_coverage", 0),
            # Honest scope of the sample: which events it covers and how many
            # rows were left out of every rate/average as price-data errors.
            "first_event_utc": metrics.get("first_event_utc"),
            "last_event_utc": metrics.get("last_event_utc"),
            "excluded_implausible_moves": metrics.get("excluded_implausible_moves", 0),
            "max_plausible_move_pct": metrics.get("max_plausible_move_pct"),
            "overall": metrics.get("overall", {}),
            "per_verdict": metrics.get("per_verdict", {}),
            "per_direction": metrics.get("per_direction", metrics.get("per_verdict", {})),
            "per_ai_verdict": metrics.get("per_ai_verdict", {}),
            "per_band": metrics.get("per_band", []),
            "per_ai_band": metrics.get("per_ai_band", []),
            "confidence_band_source": metrics.get("confidence_band_source"),
            "top_predictions": top_prediction_metrics,
            "top_predictions_history": top_predictions_history,
            "top_predictions_pending": top_predictions_pending,
            "ai_summary": ai_summary,
            "outcomes_recent": outcomes_recent,
            "pulse": {
                "overall": pulse_metrics.get("overall", {}),
                "per_side": pulse_metrics.get("per_side", {}),
                "recent": pulse_recent,
            },
        }
        payload = _json_payload_bytes(payload_data)
        _endpoint_payload_set("pre_earnings_track_record", payload)
        return Response(content=payload, media_type="application/json")
    except Exception as e:
        logger.error(f"Error getting pre-earnings track record: {e}", exc_info=True)
        raise _safe_error()


# ---------------------------------------------------------------------------
# /api/pre-earnings/learning-report — Evidence Lab (2026-09-15).
# Walk-forward, out-of-sample verdict for every Pre-Earnings claim against
# naive baselines, plus the live shadow record of the learned model.
# Read-only: never initializes the agent, never changes trading behaviour.
# ---------------------------------------------------------------------------
@app.get("/api/pre-earnings/learning-report")
def api_pre_earnings_learning_report():
    try:
        cached = _endpoint_payload_get("pre_earnings_learning_report", ttl_seconds=300.0)
        if cached is not None:
            return Response(content=cached, media_type="application/json")
        from agents.pre_earnings_agent import get_pre_earnings_agent_if_ready

        agent = get_pre_earnings_agent_if_ready()
        if agent is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "Pre-Earnings agent not ready"},
                headers={"Retry-After": "5", "Cache-Control": "no-store"},
            )
        payload = _json_payload_bytes(agent.get_learning_report())
        _endpoint_payload_set("pre_earnings_learning_report", payload)
        return Response(content=payload, media_type="application/json")
    except Exception as e:
        logger.error(f"Error building pre-earnings learning report: {e}", exc_info=True)
        raise _safe_error()


# ---------------------------------------------------------------------------
# /api/evidence/paper-performance — realized paper P&L per strategy (2026-09-15).
# Net of configured cost estimates; verdicts carry bootstrap uncertainty.
# Read-only over the decision ledger.
# ---------------------------------------------------------------------------
@app.get("/api/evidence/paper-performance")
def api_evidence_paper_performance():
    try:
        cached = _endpoint_payload_get("evidence_paper_performance", ttl_seconds=120.0)
        if cached is not None:
            return Response(content=cached, media_type="application/json")
        from core.decision_ledger.store import get_decision_ledger_store
        from core.paper_performance import summarize_paper_trades

        trades = get_decision_ledger_store().list_position_close_trades()
        payload = _json_payload_bytes(summarize_paper_trades(trades))
        _endpoint_payload_set("evidence_paper_performance", payload)
        return Response(content=payload, media_type="application/json")
    except Exception as e:
        logger.error(f"Error building paper performance evidence: {e}", exc_info=True)
        raise _safe_error()


# ---------------------------------------------------------------------------
# /api/pre-earnings/sector-refresh — History sector resolution (owner
# requirement, 2026-08-04): for a history row stuck at UNKNOWN sector, run the
# verified chain (yfinance estimates payload -> AI classification from cached
# ticker news). A name with no news and no verified sector honestly stays
# UNKNOWN. On success the sector is persisted onto every UNKNOWN row for the
# symbol (outcomes + open predictions) and the peer grouping is re-derived.
# ---------------------------------------------------------------------------
@app.post("/api/pre-earnings/sector-refresh")
async def api_pre_earnings_sector_refresh(request: Request):
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON object required")
        symbol = _normalize_pre_earnings_symbol(body.get("symbol"))
        if not _agent_enabled("pre_earnings", default=False):
            raise HTTPException(status_code=409, detail="agent disabled")

        from agents.pre_earnings_agent import get_pre_earnings_agent

        agent = get_pre_earnings_agent()
        cfg = agent._get_config() if hasattr(agent, "_get_config") else {}

        sector, source = agent.resolve_history_sector(symbol, cfg, allow_ai=True)
        if not sector:
            return {
                "symbol": symbol,
                "sector": None,
                "source": "none",
                "updated_rows": 0,
                "message": "No verified sector source (no estimates data and no usable news).",
            }
        updated = agent._persist_sector_resolution(symbol, sector, cfg)
        _endpoint_payload_clear("pre_earnings_track_record")
        return {
            "symbol": symbol,
            "sector": sector,
            "source": source,
            "updated_rows": updated,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error refreshing sector for symbol: {e}", exc_info=True)
        raise _safe_error()

# --- Hotlist endpoints (live scanner-backed) ---
# The legacy HotlistModule._load_from_disk reads `change_pct`/`volume` but the
# on-disk cache stores `gap_pct` (premarket gap data, different schema) →
# KeyError → cache stays empty → all 4 endpoints returned {"count":0,"data":[]}.
# The 408-line module is never refreshed in production (update_hotlist is only
# called from __main__). Rewire these endpoints to the LIVE scanner agent
# (agents/scanner_agent.py: Yahoo screener gainers/losers/most_active + Binance
# 24h ticker), which is refreshed every ~5 minutes by the scanner loop. Schema
# is normalized to the HotlistItem.to_dict() shape for frontend compatibility.
# This is a display/data-availability fix only — win-rate neutral (§4).
_SUPPORTED_HOTLIST_CATEGORIES = ("gainers", "losers", "most_active", "trending", "volume_leaders")
_UNSUPPORTED_HOTLIST_CATEGORIES = ("premarket", "after_hours")  # no live source wired
_SCANNER_SOURCE_BY_CATEGORY = {
    "gainers": "yahoo_gainers",
    "losers": "yahoo_losers",
    "most_active": "yahoo_active",
}
# Reverse map: scanner source tag -> hotlist category. Items whose source
# is not a specific gainers/losers/active feed (e.g. Binance crypto, watchlist
# fallback) are bucketed into "trending" so the trending/volume_leaders
# endpoints still surface them, but they never leak into gainers/losers/most_active.
_SCANNER_SOURCE_TO_CATEGORY = {
    "yahoo_gainers": "gainers",
    "yahoo_losers": "losers",
    "yahoo_active": "most_active",
    "yahoo_watchlist": "most_active",
    "binance": "trending",
}


def _normalize_scanner_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize a scanner_agent hot_ticker dict to the HotlistItem.to_dict() shape.

    The category is derived from the item's `source` tag (yahoo_gainers →
    gainers, yahoo_losers → losers, yahoo_active → most_active, binance →
    trending). This is what makes the per-category filter correct.
    """
    try:
        ticker = str(item.get("symbol") or item.get("ticker") or "").upper().strip()
        if not ticker:
            return None
        change_pct = float(item.get("change_pct") or 0.0)
        price = float(item.get("price") or 0.0)
        volume = int(item.get("volume") or item.get("quoteVolume") or 0)
        name = str(item.get("name") or item.get("shortName") or ticker)
        source = item.get("source") or "scanner"
        category = _SCANNER_SOURCE_TO_CATEGORY.get(source, "trending")
        # "timestamp"/"updated" are the time the scanner OBSERVED the row. They
        # used to be stamped with the request time, so a scan from minutes (or,
        # with a stuck scanner, hours) ago always looked freshly updated.
        ts = time.time()
        as_of_raw = item.get("_as_of_utc")
        if as_of_raw:
            try:
                as_of_dt = datetime.fromisoformat(str(as_of_raw).replace("Z", "+00:00"))
                if as_of_dt.tzinfo is None:
                    as_of_dt = as_of_dt.replace(tzinfo=timezone.utc)
                ts = as_of_dt.timestamp()
            except (TypeError, ValueError):
                ts = time.time()
        return {
            "ticker": ticker,
            "name": name,
            "price": price,
            "change_pct": round(change_pct, 2),
            "volume": volume,
            "market_cap": None,
            "sector": None,
            "category": category,
            "score": round(abs(change_pct), 2),
            "timestamp": ts,
            "updated": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "source": source,
            "exchange": item.get("exchange") or "",
        }
    except Exception:
        return None


def _get_scanner_hot_tickers() -> List[Dict[str, Any]]:
    """Fetch the live scanner hot_tickers list (non-blocking). Empty on failure."""
    try:
        from agents.scanner_agent import get_scanner_agent
        agent = get_scanner_agent()
        state = agent.get_state() or {}
        hot = state.get("hot_tickers") or []
        as_of_utc = state.get("as_of_utc")
        if as_of_utc:
            # Carry the scan time on each row (copies: scanner state is shared).
            return [{**h, "_as_of_utc": as_of_utc} for h in hot if isinstance(h, dict)]
        return [h for h in hot if isinstance(h, dict)]
    except Exception as e:
        logger.error(f"Failed to fetch scanner hot_tickers for hotlist: {e}")
        return []


def _filter_hotlist_items(
    items: List[Dict[str, Any]],
    category: Optional[str] = None,
    limit: int = 50,
    min_volume: int = 0,
    sector: Optional[str] = None,
    query: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Apply category/min_volume/sector/query filters to normalized scanner items."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        cat = it.get("category") or ""
        # trending / volume_leaders span all scanner categories; others filter by source-tagged category.
        if category and category not in ("trending", "volume_leaders") and cat != category:
            continue
        # A "gainer" that is down (or a "loser" that is up) is a contradiction on
        # screen: live scanner rows tagged yahoo_gainers carried -2.5% (the feed's
        # change is the extended-hours move, tab audit 2026-11-15). Such rows stay
        # visible under trending / volume_leaders, never under the wrong label.
        if category in ("gainers", "losers"):
            try:
                _chg = float(it.get("change_pct") or 0.0)
            except (TypeError, ValueError):
                _chg = 0.0
            if (category == "gainers" and _chg <= 0) or (category == "losers" and _chg >= 0):
                continue
        if min_volume and (int(it.get("volume") or 0) < min_volume):
            continue
        if sector and (it.get("sector") or "") != sector:
            continue
        if query:
            q = query.upper().strip()
            if q not in str(it.get("ticker") or "").upper() and q not in str(it.get("name") or "").upper():
                continue
        tk = it.get("ticker")
        if tk in seen:
            continue
        seen.add(tk)
        out.append(it)
    if category == "trending":
        out.sort(key=lambda x: abs(float(x.get("change_pct") or 0.0)), reverse=True)
    elif category == "volume_leaders":
        out.sort(key=lambda x: int(x.get("volume") or 0), reverse=True)
    else:
        out.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
    return out[:limit]


def _build_hotlist_response(
    category: Optional[str] = None,
    limit: int = 50,
    min_volume: int = 0,
    sector: Optional[str] = None,
    query: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the response for the hotlist endpoints from live scanner data."""
    timestamp = datetime.now(timezone.utc).isoformat()
    if category in _UNSUPPORTED_HOTLIST_CATEGORIES:
        return {
            "status": "success",
            "category": category,
            "available": False,
            "reason": "no_live_source",
            "count": 0,
            "data": [],
            "timestamp": timestamp,
            "source": "scanner",
        }
    raw = _get_scanner_hot_tickers()
    if not raw:
        # Scanner warming up or Yahoo screener rate-limited. Surface a clear
        # flag instead of silent count=0 so the UI can distinguish "no data
        # source wired" from "transiently empty".
        return {
            "status": "success",
            "category": category,
            "available": False,
            "reason": "scanner_warming_up",
            "count": 0,
            "data": [],
            "timestamp": timestamp,
            "source": "scanner",
        }
    normalized = [_normalize_scanner_item(it) for it in raw]
    normalized = [n for n in normalized if n is not None]
    items = _filter_hotlist_items(
        normalized, category=category, limit=limit, min_volume=min_volume, sector=sector, query=query
    )
    return {
        "status": "success",
        "category": category,
        "available": True,
        "count": len(items),
        "data": items,
        "timestamp": timestamp,
        "source": "scanner",
    }


@app.get("/api/hotlist")
def api_hotlist_categories():
    """Get all hotlist categories (backed by live scanner data)."""
    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        raw = _get_scanner_hot_tickers()
        if not raw:
            return {
                "status": "success",
                "available": False,
                "reason": "scanner_warming_up",
                "data": {cat: [] for cat in _SUPPORTED_HOTLIST_CATEGORIES + _UNSUPPORTED_HOTLIST_CATEGORIES},
                "timestamp": timestamp,
                "source": "scanner",
            }
        normalized = [_normalize_scanner_item(it) for it in raw]
        normalized = [n for n in normalized if n is not None]
        data: Dict[str, List[Dict[str, Any]]] = {}
        for cat in _SUPPORTED_HOTLIST_CATEGORIES:
            data[cat] = _filter_hotlist_items(normalized, category=cat, limit=20)
        for cat in _UNSUPPORTED_HOTLIST_CATEGORIES:
            data[cat] = []
        return {
            "status": "success",
            "available": True,
            "data": data,
            "timestamp": timestamp,
            "source": "scanner",
        }
    except Exception as e:
        logger.error(f"Error getting hotlist categories: {e}", exc_info=True)
        raise _safe_error()

@app.get("/api/hotlist/trending")
def api_hotlist_trending(limit: int = 20):
    """Get trending tickers across all scanner categories (ranked by abs change_pct)."""
    try:
        return _build_hotlist_response(category="trending", limit=limit)
    except Exception as e:
        logger.error(f"Error getting trending tickers: {e}", exc_info=True)
        raise _safe_error()

@app.get("/api/hotlist/search")
def api_hotlist_search(query: str, limit: int = 10):
    """Search hotlist by ticker or company name (case-insensitive)."""
    try:
        if not query or not query.strip():
            return {
                "status": "success",
                "query": query,
                "count": 0,
                "data": [],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "scanner",
            }
        resp = _build_hotlist_response(category="trending", limit=limit, query=query)
        resp["query"] = query
        return resp
    except Exception as e:
        logger.error(f"Error searching hotlist: {e}", exc_info=True)
        raise _safe_error()


# Backtest endpoints




# News endpoints
# /api/news/v2 endpoints removed: zombie pipeline that served a stale disk
# cache (fetch_rss_feeds was never called). Live news is /api/news-tab/*.


# ==================== Original Hotlist State Endpoint ====================

@app.get("/api/hotlist/state")
def api_hotlist_state():
    """Get hotlist state with scored tickers (singleflight-protected)"""
    try:
        # --- endpoint-level response cache (10s TTL) ---
        cached = _endpoint_payload_get("hotlist_state", ttl_seconds=10.0)
        if cached is not None:
            return Response(content=cached, media_type="application/json")

        def _build():
            from core.hotlist_scoring import get_hotlist_state
            return get_hotlist_state()
        hotlist_epoch = _hotlist_membership_epoch()
        result = hotlist_flight.do("hotlist_state", _build)

        try:
            payload_bytes = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
            if hotlist_epoch == _hotlist_membership_epoch():
                _endpoint_payload_set("hotlist_state", payload_bytes)
        except Exception:
            pass
        return result
    except Exception as e:
        logger.error(f"Error getting hotlist state: {e}")
        # "error" lets the UI tell a failed build from a genuinely empty list
        # (an empty 200 used to be indistinguishable from "no hot tickers").
        return {"hotlist": [], "hot_list": [], "last_update": None, "error": "hotlist_state_unavailable"}


@app.get("/api/hotlist/heat")
def api_hotlist_heat():
    """Curated hot list + scanner heat merged into one master feed."""
    try:
        from api.hotlist_service import build_heat_list

        return build_heat_list()
    except Exception as e:
        logger.error(f"Error building hotlist heat: {e}", exc_info=True)
        # "error" keeps a failed build from being rendered as "No heat right now".
        return {"curated": [], "heat": [], "scanner_available": False, "as_of": None,
                "error": "heat_unavailable"}


@app.get("/api/hotlist/ticker/{ticker}/dossier")
def api_hotlist_ticker_dossier(ticker: str):
    """Everything the system knows about one hot ticker (bounded, cached 60s)."""
    try:
        from api.hotlist_service import build_ticker_dossier

        return build_ticker_dossier(ticker)
    except Exception as e:
        logger.error(f"Error building dossier for {ticker}: {e}", exc_info=True)
        return {"available": False, "error": "dossier_failed", "ticker": ticker}


@app.post("/api/hotlist/ticker/{ticker}/deep-analysis")
def api_hotlist_ticker_deep_analysis(ticker: str, force: bool = False):
    """On-demand bull/bear debate + research-manager verdict for a hot ticker."""
    try:
        from api.hotlist_service import run_deep_analysis
        from api.state_service import get_shared_config

        return run_deep_analysis(ticker, config=get_shared_config(), force=bool(force))
    except Exception as e:
        logger.error(f"Error running deep analysis for {ticker}: {e}", exc_info=True)
        return {"available": False, "error": "deep_analysis_failed", "ticker": ticker}


# NOTE: registered after the literal /api/hotlist/* routes so it cannot shadow them
@app.get("/api/hotlist/{category}")
def api_hotlist_category(
    category: str,
    limit: int = 50,
    min_volume: int = 0,
    sector: Optional[str] = None,
):
    """Get specific hotlist category (backed by live scanner data)."""
    try:
        cat_lower = category.lower()
        valid = _SUPPORTED_HOTLIST_CATEGORIES + _UNSUPPORTED_HOTLIST_CATEGORIES
        if cat_lower not in valid:
            raise HTTPException(status_code=400, detail=f"Invalid category: {category}")
        return _build_hotlist_response(
            category=cat_lower, limit=limit, min_volume=min_volume, sector=sector
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting hotlist category {category}: {e}", exc_info=True)
        raise _safe_error()

# ==================== Static Files & Frontend ====================

# Get the frontend directory path
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# Mount static files
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.get("/")
async def serve_frontend():
    """Serve the main frontend page"""
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        # no-cache (revalidate every load) so the entry point is never served
        # stale. Otherwise the browser heuristically caches index.html and keeps
        # loading an old version that lacks newly-added ?v= stylesheet/script
        # links — i.e. the UI looks "stuck" on an old build after a deploy.
        return FileResponse(str(index_path), headers={"Cache-Control": "no-cache, must-revalidate"})
    raise HTTPException(status_code=404, detail="Frontend not found")

@app.get("/frontend")
@app.get("/frontend/")
@app.get("/frontend/index.html")
async def serve_frontend_compat_index():
    """Compatibility route: some clients open /frontend/index.html directly."""
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path), headers={"Cache-Control": "no-cache, must-revalidate"})
    raise HTTPException(status_code=404, detail="Frontend not found")

@app.get("/frontend/{filename:path}")
async def serve_frontend_compat_file(filename: str):
    """Compatibility static route for assets referenced from /frontend/* URLs."""
    safe_name = str(filename or "").strip().lstrip("/\\")
    if not safe_name:
        raise HTTPException(status_code=404, detail="File not found")
    file_path = FRONTEND_DIR / safe_name
    try:
        file_path.resolve().relative_to(FRONTEND_DIR.resolve())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if file_path.exists() and file_path.is_file():
        return FileResponse(str(file_path))
    raise HTTPException(status_code=404, detail="File not found")

@app.get("/favicon.ico")
async def favicon():
    """Return empty favicon to prevent 404 errors"""
    return Response(content=b"", media_type="image/x-icon")

@app.get("/api/health")
def api_health():
    """Lightweight liveness probe (no external deps)."""
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/metrics")
def api_openmetrics():
    """Authenticated, cache-free OpenMetrics scrape with bounded labels."""
    from core.openmetrics_exporter import (
        OPENMETRICS_CONTENT_TYPE,
        render_openmetrics,
    )

    return Response(
        content=render_openmetrics(),
        media_type=OPENMETRICS_CONTENT_TYPE,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/ops/slo")
def api_service_level_objectives():
    """Return the rolling API SLO evaluation used by monitoring alerts."""
    return snapshot_http_slo()


@app.get("/api/health/diagnostics")
async def api_health_diagnostics(fresh: bool = False):
    """Comprehensive diagnostics payload for production-readiness checks."""
    if not fresh:
        cached = _endpoint_payload_get("health_diagnostics", ttl_seconds=5.0)
        if cached is not None:
            return Response(content=cached, media_type="application/json")
    loop = asyncio.get_running_loop()
    lag_start = loop.time()
    await asyncio.sleep(0)
    event_loop_lag_ms = max(0.0, (loop.time() - lag_start) * 1000.0)

    # The body reads agent state, SQLite and a TCP probe (2.9-4.5 s measured
    # 2026-11-15). Run on the event loop it froze EVERY request for that long;
    # a worker thread keeps the loop free.
    payload = await asyncio.to_thread(_build_health_diagnostics_payload, event_loop_lag_ms)
    return Response(content=payload, media_type="application/json")


def _storage_health_snapshot() -> Dict[str, Any]:
    """Write-path health of the persistence layer (lost writes are never silent)."""
    snapshot: Dict[str, Any] = {"sqlite": {}, "state_store": {}, "status": "ok", "problems": []}
    try:
        snapshot["sqlite"] = get_sqlite_store().write_health()
        age = snapshot["sqlite"].get("last_write_error_age_seconds")
        if age is not None and float(age) <= 600.0:
            snapshot["status"] = "degraded"
            snapshot["problems"].append(
                f"SQLite write failed {int(float(age))}s ago for key "
                f"'{snapshot['sqlite'].get('last_write_error_key')}': "
                f"{snapshot['sqlite'].get('last_write_error')}"
            )
    except Exception as exc:
        snapshot["status"] = "degraded"
        snapshot["problems"].append(f"SQLite store unavailable: {exc}")
    try:
        from core.state_store import get_state_store

        namespaces = (get_state_store().diagnostics() or {}).get("namespaces") or {}
        snapshot["state_store"] = {
            name: {
                "dirty": bool(row.get("dirty")),
                "flush_failures": int(row.get("flush_failures") or 0),
                "last_flush_error": row.get("last_flush_error"),
            }
            for name, row in namespaces.items()
            if row.get("loaded") or row.get("flush_failures")
        }
        stuck = [
            name
            for name, row in snapshot["state_store"].items()
            if row["dirty"] and row["flush_failures"] > 0
        ]
        if stuck:
            snapshot["status"] = "degraded"
            snapshot["problems"].append(
                "State snapshot not persisted (retrying): " + ", ".join(sorted(stuck))
            )
    except Exception as exc:
        snapshot["problems"].append(f"State store diagnostics unavailable: {exc}")
    return snapshot


def _build_health_diagnostics_payload(event_loop_lag_ms: float) -> bytes:
    req_metrics = snapshot_request_metrics()
    cache_inventory = cache_manager.inventory()
    cache_inventory["runtime"] = _runtime_cache_inventory()
    cache_decisions = snapshot_cache_decisions(limit=100)
    external_traces = snapshot_external_traces(limit=100)
    runtime_agents = snapshot_agent_status()
    external_connectivity = _external_connectivity_snapshot()
    http_governor = snapshot_http_governor()
    api_traffic = _traffic_controller.snapshot()

    agents_payload = {}
    try:
        from agents.news_tab_agent import get_news_tab_agent_status

        news_status = get_news_tab_agent_status()
        update_agent_status(
            "news_tab",
            enabled=bool(news_status.get("enabled")),
            last_error=news_status.get("error"),
        )
    except Exception:
        pass

    try:
        from agents.premarket_scanner_agent import get_premarket_scanner_agent_if_ready

        pre = get_premarket_scanner_agent_if_ready()
        if pre is not None:
            state = pre.get_state() if hasattr(pre, "get_state") else {}
            st = (state or {}).get("status", {}) if isinstance(state, dict) else {}
            update_agent_status(
                "premarket_scanner",
                enabled=not bool((state or {}).get("disabled", False)),
                last_run=st.get("last_run_time"),
                last_success=st.get("last_success_time"),
                last_error=st.get("last_error"),
                next_run_in=st.get("next_run_in"),
            )
    except Exception:
        pass

    try:
        from agents.trade_radar_agent import get_trade_radar_agent_if_ready

        tr = get_trade_radar_agent_if_ready()
        if tr is not None:
            tr_state = tr.get_state(limit=1) if hasattr(tr, "get_state") else {}
            cfg = (tr_state or {}).get("config", {}) if isinstance(tr_state, dict) else {}
            update_agent_status(
                "trade_radar",
                enabled=_agent_enabled("trade_radar"),
                next_run_in=int((cfg or {}).get("radar_interval_sec", 300)),
            )
    except Exception:
        pass

    try:
        from agents.strategy_research_agent import get_strategy_research_agent_if_ready

        sr = get_strategy_research_agent_if_ready()
        if sr is not None:
            update_agent_status(
                "strategy_research",
                enabled=_agent_enabled("strategy_research"),
            )
    except Exception:
        pass

    try:
        from agents.pre_earnings_agent import get_pre_earnings_agent_if_ready

        pe = get_pre_earnings_agent_if_ready()
        if pe is not None:
            pe_state = pe.get_state() if hasattr(pe, "get_state") else {}
            pe_st = (pe_state or {}).get("status", {}) if isinstance(pe_state, dict) else {}
            update_agent_status(
                "pre_earnings",
                enabled=not bool((pe_state or {}).get("disabled", False)),
                last_run=pe_st.get("last_run_time"),
                last_success=pe_st.get("last_success_time"),
                last_error=pe_st.get("last_error"),
                next_run_in=pe_st.get("next_run_in"),
            )
    except Exception:
        pass

    update_agent_status("economic_calendar", enabled=_agent_enabled("economic_calendar"))
    update_agent_status("ollama", enabled=True)

    runtime_agents = snapshot_agent_status()
    for name, details in runtime_agents.items():
        agents_payload[name] = {
            "enabled": bool(details.get("enabled", False)),
            "last_run": details.get("last_run"),
            "last_success": details.get("last_success"),
            "last_error": details.get("last_error"),
            "avg_duration_ms": details.get("avg_duration_ms"),
            "next_run_in": details.get("next_run_in"),
            "last_run_external_fetches": details.get("last_run_external_fetches"),
            "last_run_external_budget": details.get("last_run_external_budget"),
            "last_run_external_blocked": details.get("last_run_external_blocked"),
            "last_run_external_by_provider": details.get("last_run_external_by_provider"),
            "last_run_cache_hits": details.get("last_run_cache_hits"),
            "last_run_cache_misses": details.get("last_run_cache_misses"),
            "last_run_cache_hit_rate": details.get("last_run_cache_hit_rate"),
        }

    try:
        from core.disaster_recovery import get_backup_scheduler

        backup_status = get_backup_scheduler().status()
    except Exception as exc:
        logger.error("Unable to read disaster-recovery status: %s", exc)
        backup_status = {
            "enabled": False,
            "running": False,
            "last_error": "Backup status unavailable.",
            "snapshot_count": 0,
            "latest_snapshot": None,
        }

    try:
        from core.operator_audit import get_operator_audit_ledger

        operator_audit_status = get_operator_audit_ledger().status()
    except Exception as exc:
        logger.error("Unable to read operator-audit status: %s", exc)
        operator_audit_status = {
            "enabled": False,
            "required": _IS_PRODUCTION,
            "ready": False,
            "record_count": 0,
            "last_error": "Operator audit status unavailable.",
        }

    status = "ok"
    degraded_reasons = []
    if operator_audit_status.get("required") and not operator_audit_status.get("ready"):
        status = "degraded"
        degraded_reasons.append({
            "code": "operator_audit_unavailable",
            "message": "The tamper-evident operator audit ledger is unavailable.",
        })
    if backup_status.get("enabled") and backup_status.get("last_error"):
        status = "degraded"
        degraded_reasons.append({
            "code": "backup_scheduler_error",
            "message": "The disaster-recovery backup scheduler reports an error.",
        })
    enabled_with_error = [
        name
        for name, details in agents_payload.items()
        if details.get("enabled") and details.get("last_error")
    ]
    if enabled_with_error:
        status = "degraded"
        degraded_reasons.append({
            "code": "agent_error",
            "agents": enabled_with_error,
            "message": "One or more enabled agents report a runtime error.",
        })
    budget_exhausted_agents = {
        name: int(details.get("last_run_external_blocked") or 0)
        for name, details in agents_payload.items()
        if details.get("enabled")
        and int(details.get("last_run_external_blocked") or 0) > 0
    }
    if budget_exhausted_agents:
        status = "degraded"
        degraded_reasons.append({
            "code": "agent_external_budget_exhausted",
            "agents": budget_exhausted_agents,
            "message": "One or more agents exhausted their external-call budget and used partial/cache-only data.",
        })
    if event_loop_lag_ms > 250:
        status = "degraded"
        degraded_reasons.append({
            "code": "event_loop_lag",
            "lag_ms": round(event_loop_lag_ms, 3),
            "threshold_ms": 250,
            "message": "The API event loop is responding too slowly.",
        })
    if bool(external_connectivity.get("enabled")) and int(external_connectivity.get("connected_providers", 0)) == 0:
        status = "degraded"
        degraded_reasons.append({
            "code": "external_connectivity",
            "message": "No configured external provider passed the connectivity probe.",
        })
    active_endpoint_cooldowns = list(http_governor.get("endpoint_cooldowns") or [])
    critical_cooldowns = [
        item
        for item in active_endpoint_cooldowns
        if str(item.get("provider") or "").lower()
        in {"alpaca", "binance", "finnhub", "kucoin", "marketaux", "yahoo"}
    ]
    if critical_cooldowns:
        status = "degraded"
        degraded_reasons.append({
            "code": "upstream_endpoint_cooldown",
            "providers": sorted({
                str(item.get("provider") or "unknown")
                for item in critical_cooldowns
            }),
            "count": len(critical_cooldowns),
            "message": "One or more upstream endpoints are temporarily suppressed after a permanent failure.",
        })

    # NEW: latency percentiles, error counts, cache hit rates
    latency_stats = snapshot_latency_percentiles(window_sec=60.0)
    error_stats = snapshot_error_counts()
    cache_hit_stats = snapshot_cache_hit_rates()
    slo_status = snapshot_http_slo()
    if slo_status.get("enough_data") and slo_status.get("compliant") is False:
        status = "degraded"
        degraded_reasons.append({
            "code": "service_level_objective_breached",
            "availability_ratio": slo_status.get("availability_ratio"),
            "availability_target_ratio": slo_status.get(
                "availability_target_ratio"
            ),
            "p95_ms": slo_status.get("p95_ms"),
            "p95_target_ms": slo_status.get("p95_target_ms"),
            "message": "The rolling API availability or latency SLO is breached.",
        })

    # Detect degraded latency: p95 > 500ms on hot endpoints
    hot_endpoints = {"/api/state", "/api/dashboard", "/api/live-prices",
                     "/api/news-tab/state", "/api/premarket", "/api/scanner/state"}
    for ep, lats in latency_stats.get("per_endpoint", {}).items():
        if ep in hot_endpoints and lats.get("p95_ms", 0) > 500:
            status = "degraded"
            degraded_reasons.append({
                "code": "hot_endpoint_latency",
                "endpoint": ep,
                "p95_ms": round(float(lats.get("p95_ms", 0)), 3),
                "threshold_ms": 500,
                "message": "A hot UI endpoint exceeded its p95 latency budget.",
            })
            break

    storage_health = _storage_health_snapshot()
    if storage_health.get("status") != "ok":
        status = "degraded"
        degraded_reasons.append({
            "code": "storage_write_failure",
            "problems": list(storage_health.get("problems") or [])[:5],
            "message": "A state write failed or is still pending; recent changes may not be persisted.",
        })
    try:
        from core.stock_data_client import missing_symbols_snapshot

        yahoo_missing_symbols = missing_symbols_snapshot()
    except Exception:
        yahoo_missing_symbols = {}

    payload_obj = {
        "status": status,
        "degraded_reasons": degraded_reasons,
        "uptime_seconds": round(uptime_seconds(), 3),
        "version": app_version(),
        "commit_hash": commit_hash(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "test_mode": IS_TEST_MODE,
        "agents": agents_payload,
        "cache_inventory": cache_inventory,
        "request_metrics": req_metrics,
        "latency": latency_stats,
        "errors": error_stats,
        "cache_hit_rates": cache_hit_stats,
        "service_level_objectives": slo_status,
        "event_loop": {
            "lag_ms": round(event_loop_lag_ms, 3),
        },
        "thread_pool": {
            "active_threads": threading.active_count(),
            "thread_names_sample": [t.name for t in threading.enumerate()[:20]],
        },
        "memory": memory_snapshot(),
        "external_connectivity": external_connectivity,
        "http_governor": http_governor,
        "api_traffic": api_traffic,
        "external_traces": external_traces,
        "cache_decisions": cache_decisions,
        "data_health": get_health_dashboard().get_live_dashboard(),
        "disaster_recovery": backup_status,
        "operator_audit": operator_audit_status,
        "storage": storage_health,
        # Symbols Yahoo answered "unknown" for (delisted watchlist tickers,
        # exchange pairs): not asked again until the retry time shown.
        "yahoo_missing_symbols": yahoo_missing_symbols,
    }
    payload = json.dumps(payload_obj, ensure_ascii=False, default=str).encode("utf-8")
    _endpoint_payload_set("health_diagnostics", payload)
    return payload

@app.get("/api/ready")
def api_ready():
    """Readiness probe (checks SQLite keys and JSON mirrors)."""
    from core.config import STATE_PATH, SIGNALS_PATH
    state_exists = STATE_PATH.exists()
    signals_exists = SIGNALS_PATH.exists()
    try:
        store = get_sqlite_store()
        state_exists = state_exists or store.get_text(SQLITE_KEY_STATE, None) is not None
        signals_exists = signals_exists or store.get_text(SQLITE_KEY_SIGNALS, None) is not None
    except Exception:
        pass
    return {
        "ready": bool(state_exists and signals_exists),
        "state_exists": state_exists,
        "signals_exists": signals_exists,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def _backup_archive_path(backup_name: str) -> Path:
    from core.disaster_recovery import get_backup_scheduler

    safe_name = str(backup_name or "").strip()
    if not safe_name or Path(safe_name).name != safe_name:
        raise HTTPException(status_code=400, detail="Invalid backup name.")
    manager = get_backup_scheduler().manager
    candidate = (manager.backup_dir / safe_name).resolve()
    try:
        candidate.relative_to(manager.backup_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid backup name.")
    return candidate


@app.get("/api/ops/backups")
def api_list_backups(verify: bool = False, limit: int = 20):
    """List local recovery points without exposing restore over HTTP."""
    from core.disaster_recovery import get_backup_scheduler

    scheduler = get_backup_scheduler()
    safe_limit = max(1, min(int(limit), 100))
    return {
        "status": scheduler.status(),
        "backups": scheduler.manager.list_snapshots(verify=bool(verify))[:safe_limit],
    }


@app.post("/api/ops/backups")
def api_create_backup(request: BackupCreateRequest):
    """Create and verify an on-demand recovery point."""
    from core.disaster_recovery import BackupError, get_backup_scheduler

    try:
        return get_backup_scheduler().manager.create_snapshot(label=request.label)
    except BackupError as exc:
        logger.error("Manual backup failed: %s", exc)
        raise HTTPException(status_code=503, detail="Backup creation failed.")


@app.post("/api/ops/backups/{backup_name}/verify")
def api_verify_backup(backup_name: str):
    """Verify archive hashes and SQLite integrity; restoration remains CLI-only."""
    from core.disaster_recovery import BackupError, get_backup_scheduler

    try:
        result = get_backup_scheduler().manager.verify_snapshot(
            _backup_archive_path(backup_name)
        )
        result.pop("manifest", None)
        return result
    except BackupError as exc:
        logger.warning("Backup verification failed for %s: %s", backup_name, exc)
        raise HTTPException(status_code=422, detail="Backup verification failed.")


@app.get("/api/ops/audit/status")
def api_operator_audit_status():
    """Expose audit-chain readiness without returning operator event records."""
    from core.operator_audit import get_operator_audit_ledger

    return get_operator_audit_ledger().status()


@app.post("/api/ops/audit/verify")
def api_verify_operator_audit():
    """Force a complete HMAC-chain verification."""
    from core.operator_audit import get_operator_audit_ledger

    result = get_operator_audit_ledger().verify()
    if not result.get("verified"):
        return JSONResponse(
            status_code=409,
            headers={"Cache-Control": "no-store"},
            content=result,
        )
    return result


@app.get("/{filename}")
async def serve_static_file(filename: str):
    """Serve static files from frontend directory"""
    safe_name = str(filename or "").strip().lstrip("/\\")
    if not safe_name:
        raise HTTPException(status_code=404, detail="File not found")
    file_path = FRONTEND_DIR / safe_name
    try:
        file_path.resolve().relative_to(FRONTEND_DIR.resolve())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if file_path.exists() and file_path.is_file():
        return FileResponse(str(file_path))
    raise HTTPException(status_code=404, detail="File not found")

# ==================== Engine Control ====================

@app.get("/api/engine/status")
def api_engine_status():
    """Get engine status"""
    return get_engine_status()

@app.post("/api/engine/start")
def api_start_engine():
    """Start the background engine"""
    success = start_engine()
    return {"status": "started" if success else "failed"}

@app.post("/api/engine/stop")
def api_stop_engine():
    """Stop the background engine"""
    stop_engine()
    return {"status": "stopped"}


# ── Bot Controller ─────────────────────────────────────────────────────────
def _get_bot_controller():
    from core.bot_controller import BotController
    from api.state_service import get_shared_config

    config_manager = get_shared_config()
    controller = BotController.get_instance(config_manager=config_manager)
    try:
        from api.engine_service import get_engine

        engine = get_engine()
        if engine and hasattr(engine, "agent_manager"):
            controller.agent_manager = engine.agent_manager
    except Exception as exc:
        logger.warning("Failed to wire agent_manager from engine: %s", exc)
    return controller


@app.post("/api/bot/start")
def api_bot_start():
    """Start autonomous trading bot (paper mode by default)"""
    try:
        if not is_engine_running():
            raise HTTPException(status_code=409, detail="Market engine must be running before the bot can start")
        from api.state_service import get_shared_config

        agents_config = get_shared_config().agents or {}
        missing = [
            name
            for name in ("execution", "risk")
            if not bool((agents_config.get(name) or {}).get("enabled", False))
        ]
        if missing:
            raise HTTPException(
                status_code=409,
                detail=f"Enable required agents before starting the bot: {', '.join(missing)}",
            )
        result = _get_bot_controller().start()
        # bot.enabled is how the engine process learns the bot is on. Only a
        # start that wrote it is checked ("blocked"/"error" results write nothing).
        if isinstance(result, dict) and result.get("status") in {"started", "resumed"}:
            _raise_if_config_not_saved(get_shared_config(), "Bot start")
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Bot start failed")
        raise HTTPException(status_code=500, detail=f"Bot start failed: {exc}") from exc


@app.post("/api/bot/stop")
def api_bot_stop():
    """Stop autonomous trading bot"""
    try:
        result = _get_bot_controller().stop(reason="api_request")
        # If bot.enabled=False did not reach config.json the engine process
        # still sees the bot as enabled: that is not a successful stop.
        from api.state_service import get_shared_config
        _raise_if_config_not_saved(get_shared_config(), "Bot stop")
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Bot stop failed")
        raise HTTPException(status_code=500, detail=f"Bot stop failed: {exc}") from exc


@app.get("/api/bot/status")
def api_bot_status():
    """Get bot status including circuit breaker state"""
    try:
        status = _get_bot_controller().get_status()
        status["engine_running"] = is_engine_running()
        return status
    except Exception as exc:
        logger.exception("Bot status failed")
        raise HTTPException(status_code=500, detail=f"Bot status failed: {exc}") from exc


@app.get("/api/bot/positions")
def api_bot_positions():
    """Get open positions with trailing SL, unrealized PnL, and exit manager status"""
    import concurrent.futures
    from core.capital_manager import get_capital_manager
    cm = get_capital_manager()
    positions = cm.get_positions() if hasattr(cm, "get_positions") else []

    # Filter open positions first
    open_raw = [dict(p) for p in positions if str(p.get("status", "")).upper() == "OPEN"]
    if not open_raw:
        return {"positions": [], "count": 0}

    def _verified_exec_price(exec_data):
        if not isinstance(exec_data, dict):
            return False
        source = str(exec_data.get("source") or "").strip().lower()
        if source == "fallback":
            return False
        quality_meta = exec_data.get("execution_price_quality") or {}
        if not isinstance(quality_meta, dict):
            return False
        level = str(quality_meta.get("level") or quality_meta.get("quality") or "").strip().upper()
        if quality_meta.get("is_valid") is False:
            return False
        try:
            return level == "VERIFIED" and float(exec_data.get("execution_price") or 0.0) > 0
        except Exception:
            return False

    # Fetch prices concurrently (8s timeout per ticker, max 10 workers)
    def _fetch(pos):
        t = pos.get("ticker", "")
        side = "SELL" if str(pos.get("direction", "LONG")).upper() == "LONG" else "BUY"
        try:
            return (t, cm._get_execution_price(t, side, use_cache=False))
        except Exception:
            return (t, None)

    price_map = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(open_raw), 10)) as pool:
        futures = {pool.submit(_fetch, p): p for p in open_raw}
        for future in concurrent.futures.as_completed(futures, timeout=30):
            try:
                ticker_key, exec_data = future.result(timeout=5)
                if exec_data:
                    price_map[ticker_key] = exec_data
            except Exception:
                pass

    open_positions = []
    for pos in open_raw:
        ticker = pos.get("ticker", "")
        direction = str(pos.get("direction", "LONG")).upper()
        exec_data = price_map.get(ticker)
        if _verified_exec_price(exec_data):
            curr_price = exec_data["execution_price"]
            entry = float(pos.get("entry_price", 0.0))
            qty = float(pos.get("qty", 0.0))
            if entry > 0 and qty > 0:
                if direction == "LONG":
                    unrealized = (curr_price - entry) * qty
                else:
                    unrealized = (entry - curr_price) * qty
                pos["current_price"] = round(curr_price, 8)
                pos["current_price_live"] = True
                pos["current_price_side"] = "SELL" if direction == "LONG" else "BUY"
                pos["current_price_source"] = str(exec_data.get("source") or "")
                pos["current_price_timestamp"] = str(exec_data.get("price_timestamp") or "")
                pos["current_price_quality"] = exec_data.get("execution_price_quality") or {}
                pos["unrealized_pnl"] = round(unrealized, 4)
                pos["unrealized_pnl_pct"] = round(unrealized / (entry * qty) * 100, 2) if entry * qty > 0 else 0.0
            else:
                pos["current_price"] = 0.0
                pos["current_price_live"] = False
                pos["unrealized_pnl"] = 0.0
                pos["unrealized_pnl_pct"] = 0.0
        else:
            pos["current_price"] = 0.0
            pos["current_price_live"] = False
            if isinstance(exec_data, dict):
                pos["current_price_source"] = str(exec_data.get("source") or "unavailable")
            pos["unrealized_pnl"] = 0.0
            pos["unrealized_pnl_pct"] = 0.0

        pos["trailing_sl"] = float(pos.get("trailing_sl", 0.0))
        pos["max_favorable_excursion"] = float(pos.get("max_favorable_excursion", 0.0))
        open_positions.append(pos)
    return {"positions": open_positions, "count": len(open_positions)}


@app.get("/api/bot/health")
def api_bot_health():
    """Get bot health report: portfolio metrics, strategy performance, risk status"""
    from core.capital_manager import get_capital_manager
    from core.feedback_loop import compute_portfolio_metrics, get_verified_capital_trades
    from core.bot_controller import BotController

    positions = get_capital_manager().get_positions()
    trades = get_verified_capital_trades(50, positions=positions)
    closed_positions = [
        position for position in positions
        if isinstance(position, dict) and str(position.get("status") or "").upper() == "CLOSED"
    ]
    metrics = compute_portfolio_metrics(trades)

    bc = BotController.get_instance()
    bot_status = bc.get_status() if bc else {"running": False}

    return {
        "bot_status": bot_status,
        "portfolio_metrics": metrics,
        "recent_trades_count": len(trades),
        "metric_scope": "verified automated Capital closes only; research and unverified closes excluded",
        "excluded_closed_positions": max(0, len(closed_positions) - len(trades)),
    }


@app.get("/api/bot/optimization")
def api_bot_optimization():
    """Get pending parameter optimization suggestions"""
    from core.param_optimizer import ParamOptimizer
    from core.config import ConfigManager

    optimizer = ParamOptimizer(ConfigManager())
    result = optimizer.run_optimization()
    return result


@app.post("/api/bot/apply-optimization")
def api_bot_apply_optimization():
    """Apply pending optimization suggestions (requires approval)"""
    from core.param_optimizer import ParamOptimizer
    from core.config import ConfigManager

    optimizer = ParamOptimizer(ConfigManager())
    result = optimizer.run_optimization()
    suggestions = result.get("suggestions", [])

    if not suggestions:
        return {"status": "no_suggestions", "applied": []}

    applied = optimizer.apply_suggestions(suggestions)
    return applied


@app.get("/api/bot/can-go-live")
def api_bot_can_go_live():
    """Check if bot meets requirements for live trading mode"""
    from core.bot_controller import BotController

    bc = BotController.get_instance()
    if bc is None:
        return {"eligible": False, "reasons": ["Bot not initialized"], "metrics": {}}
    return bc.can_go_live()
@app.get("/api/performance/quality")
def api_signal_quality(breakdown_by: str = "risk_mode"):
    """
    Returns win rate, profit factor, avg_R, max drawdown, expectancy_R
    for 7d, 30d, and all-time windows, with per-dimension breakdown.

    Query params:
      breakdown_by: 'risk_mode' | 'action' | 'ticker'  (default: risk_mode)

    Response fields (each window):
      win_rate, profit_factor, avg_R, expectancy_R, max_drawdown,
      wins, losses, sample_size, alerts (list of alert codes)

    Alert codes:
      win_rate_degraded    — win_rate < 48%
      profit_factor_low    — profit_factor < 1.0
      drawdown_excessive   — max_drawdown > 25%
    """
    from core.signal_quality import compute_quality_report
    import traceback

    allowed_breakdowns = {"risk_mode", "action", "ticker"}
    if breakdown_by not in allowed_breakdowns:
        breakdown_by = "risk_mode"

    try:
        # Attempt to get closed signals from the signal manager
        closed_signals: list = []
        try:
            from core.signals import get_signal_manager
            sm = get_signal_manager()
            closed_signals = list(sm.closed_signals or [])
        except Exception:
            # Fallback: try state store
            try:
                from core.state_store import state_store
                raw = state_store.get("signals", "closed_signals")
                if isinstance(raw, list):
                    closed_signals = raw
            except Exception:
                pass

        report = compute_quality_report(closed_signals, breakdown_by=breakdown_by)
        report["closed_signals_loaded"] = len(closed_signals)
        return report

    except Exception as e:
        logger.error(f"api_signal_quality failed: {e}\n{traceback.format_exc()}")
        return {
            "error": str(e),
            "as_of": None,
            "global": {"7d": {}, "30d": {}, "all_time": {}},
            "breakdown": {},
            "active_alerts": [],
        }

# Main startup/shutdown event implementations (used by lifespan)
def _sweep_orphan_tmp_files() -> int:
    """Remove atomic-write temp files leaked by hard-killed processes.

    Writers (calendar cache, watchlist, state) use mkstemp + os.replace with
    cleanup in finally, but a hard kill inside the write window leaks the temp
    file. Nothing else ever deletes them (425 MB accumulated before this sweep).
    Only files older than 1 hour are touched so in-flight writes are safe.
    """
    base = Path(__file__).parent.parent
    cutoff = time.time() - 3600
    removed = 0
    for pattern in ("calendar_cache_*.tmp", "*.json.*.tmp", "tmp*.tmp"):
        for p in base.glob(pattern):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except Exception:
                continue
    if removed:
        logger.info(f"Startup sweep removed {removed} orphaned tmp files")
    return removed


async def startup_event_impl():
    logger.info("=" * 50)
    logger.info("Market Signals V9 - Web API Starting")
    logger.info("=" * 50)
    logger.info(f"Frontend directory: {FRONTEND_DIR}")

    if IS_TEST_MODE:
        logger.info("TEST_MODE enabled: skipping engine/agent autostart and warmups.")
        os.environ.setdefault("MS_DISABLE_ENGINE_AUTOSTART", "1")
        os.environ.setdefault("MS_DISABLE_WARMUP", "1")
        os.environ.setdefault("MS_DISABLE_STATE_PREFETCH", "1")
        update_agent_status("trade_radar", enabled=False)
        update_agent_status("strategy_research", enabled=False)
        update_agent_status("premarket_scanner", enabled=False)
        update_agent_status("news_tab", enabled=_agent_enabled("news_tab"))
        update_agent_status("economic_calendar", enabled=_agent_enabled("economic_calendar"))
        return

    try:
        _sweep_orphan_tmp_files()
    except Exception as e:
        logger.warning(f"Orphan tmp sweep failed: {e}")
    # The sweep above only looks at the install folder (the pre-runtime/ layout).
    # This one logs the data/runtime folders and database this process really
    # uses and removes orphaned atomic-write temp files from those two folders.
    try:
        from core.config import startup_storage_check
        startup_storage_check("api")
    except Exception as e:
        logger.warning(f"Startup storage check failed: {e}")

    # Ensure Ollama is running (non-blocking)
    try:
        threading.Thread(target=_ensure_ollama_running, daemon=True, name="OllamaAutoStart").start()
    except Exception as e:
        logger.warning(f"Failed to trigger Ollama auto-start: {e}")

    # Start the engine in background
    if os.getenv("MS_DISABLE_ENGINE_AUTOSTART", "0") == "1":
        logger.info("Engine autostart disabled by MS_DISABLE_ENGINE_AUTOSTART=1")
    else:
        logger.info("Starting background engine...")
        start_engine()

    # Reconciliation is local, bounded and required before any new Paper Auto
    # risk. A conflict remains observable and fail-closed; API startup continues
    # so the owner can inspect and explicitly reconcile from the UI.
    try:
        from api.execution_service import reconcile_execution
        from api.state_service import get_shared_config

        execution_reconciliation = reconcile_execution(get_shared_config())
        logger.info(
            "Paper execution reconciliation: %s",
            execution_reconciliation.get("status"),
        )
    except Exception as exc:
        logger.error("Paper execution reconciliation failed closed at startup: %s", exc)

    # Restore the API-side safety monitors when the persisted master switch was
    # intentionally left on before a restart. The subprocess engine reads the same
    # switch independently.
    try:
        bot_controller = _get_bot_controller()
        if bool(bot_controller._get_bot_config().get("enabled", False)):
            result = bot_controller.start()
            logger.info("Bot controller restored from config: %s", result.get("status"))
    except Exception as exc:
        logger.error("Bot controller restore failed; disabling automation: %s", exc)
        try:
            _get_bot_controller().stop(reason="startup_restore_failed")
        except Exception:
            logger.exception("Failed to persist safe bot state after restore error")

    # §111 continuous research loop — owner-gated, research-only (no orders,
    # no risk changes, no promotion). Default OFF; enabled via
    # research_factory.auto_cycle in config. Fail-open: a crash here never
    # blocks API startup.
    try:
        from api.research_factory_service import auto_cycle_startup

        result = auto_cycle_startup()
        if result.get("started"):
            logger.info("Research auto-cycle scheduler started from config")
    except Exception as exc:
        logger.error("Research auto-cycle scheduler failed to start: %s", exc)

    # Start warmup prefetch (rate-limited)
    try:
        if os.getenv("MS_DISABLE_WARMUP", "0") == "1":
            logger.info("Warmup prefetch disabled by MS_DISABLE_WARMUP=1")
        else:
            from core.startup_warmup import get_warmup_manager
            get_warmup_manager().start()
            logger.info("Warmup prefetch started")
    except Exception as e:
        logger.error(f"Warmup prefetch failed to start: {e}")

    # Prefetch state for Watchlist/Dashboard (non-blocking)
    try:
        if os.getenv("MS_DISABLE_STATE_PREFETCH", "0") == "1":
            logger.info("State prefetch disabled by MS_DISABLE_STATE_PREFETCH=1")
        else:
            def _prefetch_state():
                try:
                    from api.state_service import get_state
                    # Quick state build to warm caches (no news, no heavy hotlist details)
                    get_state(include_news=False, include_hotlist=True, hotlist_details=False)
                    logger.info("Startup state prefetch completed")
                except Exception as e:
                    logger.error(f"Startup state prefetch failed: {e}")

            threading.Thread(target=_prefetch_state, daemon=True, name="StartupStatePrefetch").start()
    except Exception as e:
        logger.error(f"Failed to start state prefetch: {e}")

    # Delayed, gentle cache warm so the user never pays the cold-rebuild cost on first load
    # (measured: /state ~28s cold vs ~0.1s warm; /hotlist ~30s cold + occasional 500 vs ~3.6s
    # warm). A cold /state also exceeds the frontend's 25s fetch timeout, which (together with
    # the cold /hotlist 500) is what flips the UI into the "offline" banner right after a
    # restart. This runs ONCE ~25s after boot — late enough not to contend with engine/agent
    # startup (which is why the eager MS_DISABLE_STATE_PREFETCH path stays off), early enough
    # to be warm before the user interacts. Daemon + fully guarded: cannot crash or block boot.
    try:
        if IS_TEST_MODE or os.getenv("MS_DISABLE_BOOT_WARM", "0") == "1":
            logger.info("Boot cache warm disabled")
        else:
            def _delayed_cache_warm():
                import time as _t
                _t.sleep(25)
                try:
                    from api.state_service import get_state
                    get_state(include_news=False, include_hotlist=True, hotlist_details=False)
                    logger.info("Boot warm: /state cache warmed")
                except Exception as e:
                    logger.debug(f"Boot warm (state) skipped: {e}")
                try:
                    from core.hotlist_scoring import get_hotlist_state
                    get_hotlist_state()
                    logger.info("Boot warm: /hotlist cache warmed")
                except Exception as e:
                    logger.debug(f"Boot warm (hotlist) skipped: {e}")

            threading.Thread(target=_delayed_cache_warm, daemon=True, name="BootCacheWarm").start()
    except Exception as e:
        logger.debug(f"Boot cache warm failed to start: {e}")

    # Start DataHealthDashboard Cron
    def _run_health_audit_loop():
        from core.data_quality import get_health_dashboard
        import time
        from datetime import datetime, timezone
        
        dh = get_health_dashboard()
        last_run_day = None
        while True:
            try:
                now = datetime.now(timezone.utc)
                if now.hour == 6 and now.minute == 0 and now.day != last_run_day:
                    dh.perform_daily_audit()
                    last_run_day = now.day
            except Exception as e:
                logger.error(f"Health Audit loop error: {e}")
            time.sleep(30)
            
    try:
        threading.Thread(target=_run_health_audit_loop, daemon=True, name="DataHealthCron").start()
        logger.info("DataHealthCron background task started")
    except Exception as e:
        logger.error(f"Failed to start DataHealthCron: {e}")

    # Start Trade Radar agent
    try:
        if _agent_enabled("trade_radar"):
            from agents.trade_radar_agent import get_trade_radar_agent

            get_trade_radar_agent().start()
            update_agent_status("trade_radar", enabled=True, running=True, lifecycle_state="running")
            logger.info("TradeRadarAgent started from persisted Agent Fleet ON state.")
        else:
            logger.info("TradeRadarAgent autostart skipped (disabled in config.agents.trade_radar).")
    except Exception as e:
        logger.error(f"Failed to start TradeRadarAgent: {e}")

    # Start Strategy Research agent (V2 performance cache builder)
    try:
        if _agent_enabled("strategy_research"):
            from agents.strategy_research_agent import get_strategy_research_agent

            get_strategy_research_agent().start()
            update_agent_status("strategy_research", enabled=True, running=True, lifecycle_state="running")
            logger.info("StrategyResearchAgent started from persisted Agent Fleet ON state.")
        else:
            logger.info("StrategyResearchAgent autostart skipped (disabled in config.agents.strategy_research).")
    except Exception as e:
        logger.error(f"Failed to start StrategyResearchAgent: {e}")

    # Start Premarket Scanner agent
    try:
        if _agent_enabled("premarket_scanner", default=False) and not IS_TEST_MODE:
            from agents.premarket_scanner_agent import get_premarket_scanner_agent
            get_premarket_scanner_agent().start()
            logger.info("PremarketScannerAgent started (background loop).")
        elif IS_TEST_MODE:
            # Don't spawn live external-polling background loops under tests — they make
            # real provider calls that race the cache-only/external-free assertions.
            logger.info("PremarketScannerAgent autostart skipped (TEST_MODE).")
        else:
            logger.info("PremarketScannerAgent autostart skipped (disabled in config.agents.premarket_scanner).")
    except Exception as e:
        logger.error(f"Failed to start PremarketScannerAgent: {e}")

    # Start Pre-Earnings agent.
    # start() only spins a daemon refresh loop (the heavy run_cycle executes in that
    # thread, not at boot), so the persisted Agent Fleet ON state always starts it.
    # Started off-thread so it never delays startup; skipped under TEST_MODE.
    try:
        if _agent_enabled("pre_earnings", default=False) and not IS_TEST_MODE:
            update_agent_status("pre_earnings", enabled=True)

            def _start_pre_earnings():
                try:
                    from agents.pre_earnings_agent import get_pre_earnings_agent
                    get_pre_earnings_agent().start()
                    logger.info("PreEarningsAgent started (background refresh loop).")
                except Exception as e:
                    logger.error(f"PreEarningsAgent start failed: {e}")

            threading.Thread(target=_start_pre_earnings, daemon=True, name="PreEarningsAutostart").start()
        elif IS_TEST_MODE:
            logger.info("PreEarningsAgent autostart skipped (TEST_MODE).")
        else:
            logger.info("PreEarningsAgent autostart skipped (disabled in config.agents.pre_earnings).")
    except Exception as e:
        logger.error(f"Failed to start PreEarningsAgent: {e}")

    # Start News Tab agent
    try:
        if _agent_enabled("news_tab", default=True):
            from agents.news_tab_agent import kickoff_news_tab_agent_init

            kickoff_news_tab_agent_init()
            update_agent_status("news_tab", enabled=True, initializing=True, lifecycle_state="starting")
            logger.info("NewsTabAgent initialization started from persisted Agent Fleet ON state.")
        else:
            logger.info("NewsTabAgent autostart skipped (disabled in config.agents.news_tab).")
    except Exception as e:
        logger.error(f"Failed to start NewsTabAgent: {e}")

    # Start Economic Calendar scheduled cache refresh.
    try:
        if _agent_enabled("economic_calendar", default=True):
            from agents.economic_calendar_agent import get_economic_calendar_agent

            get_economic_calendar_agent(ConfigManager().config).start()
            update_agent_status("economic_calendar", enabled=True, running=True, lifecycle_state="running")
            logger.info("EconomicCalendarAgent started from persisted Agent Fleet ON state.")
        else:
            logger.info("EconomicCalendarAgent autostart skipped (disabled in config).")
    except Exception as e:
        logger.error(f"Failed to start EconomicCalendarAgent: {e}")

    # Start UI & Data Inspector Agent
    try:
        if _agent_enabled("ui_inspector", default=True):
            from agents.ui_inspector_agent import get_ui_inspector_agent

            get_ui_inspector_agent(ConfigManager().config).start()
            update_agent_status("ui_inspector", enabled=True, running=True, lifecycle_state="running")
            logger.info("UIInspectorAgent started from persisted Agent Fleet ON state.")
        else:
            logger.info("UIInspectorAgent autostart skipped (disabled in config).")
    except Exception as e:
        logger.error(f"Failed to start UIInspectorAgent: {e}")

async def shutdown_event_impl():
    logger.info("Stopping background engine...")
    try:
        from agents.ui_inspector_agent import get_ui_inspector_agent_if_ready

        inspector = get_ui_inspector_agent_if_ready()
        if inspector is not None:
            inspector.stop()
    except Exception:
        pass
    try:
        from agents.trade_radar_agent import get_trade_radar_agent_if_ready

        radar = get_trade_radar_agent_if_ready()
        if radar is not None:
            radar.stop()
    except Exception:
        pass
    try:
        from agents.strategy_research_agent import get_strategy_research_agent_if_ready

        agent = get_strategy_research_agent_if_ready()
        if agent is not None:
            agent.stop()
    except Exception:
        pass
    try:
        from agents.premarket_scanner_agent import get_premarket_scanner_agent_if_ready

        agent = get_premarket_scanner_agent_if_ready()
        if agent is not None:
            agent.stop()
    except Exception:
        pass
    try:
        from agents.pre_earnings_agent import get_pre_earnings_agent_if_ready

        agent = get_pre_earnings_agent_if_ready()
        if agent is not None:
            agent.stop()
    except Exception:
        pass
    try:
        from agents.news_tab_agent import shutdown_news_tab_agent

        shutdown_news_tab_agent()
    except Exception:
        pass
    try:
        from agents.economic_calendar_agent import get_economic_calendar_agent_if_ready

        agent = get_economic_calendar_agent_if_ready()
        if agent is not None:
            agent.stop()
    except Exception:
        pass
    stop_engine()
    try:
        from api.research_factory_service import auto_cycle_shutdown

        auto_cycle_shutdown()
    except Exception:
        pass
    try:
        from core.startup_warmup import get_warmup_manager
        get_warmup_manager().stop()
    except Exception:
        pass
    logger.info("Market Signals V9 - Web API Stopped")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
