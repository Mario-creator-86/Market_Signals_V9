
import logging
import os
import threading
import time
from copy import deepcopy
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse

import requests

from core.http_utils import http_get, http_post
from core.data_quality import get_health_dashboard, QualifiedValue

logger = logging.getLogger("OllamaClient")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_CLOUD_CATALOG_URL = "https://ollama.com/api/tags"
DEFAULT_OLLAMA_MODEL = "gemma3:12b"
DEFAULT_TIMEOUT = 120       # Background agents: full budget
UI_TIMEOUT_CAP = 15         # UI-facing analysis: never block >15s
_CONFIG_CACHE_LOCK = threading.Lock()
_CONFIG_CACHE: Dict[str, Any] = {"mtime_ns": None, "data": {}}


def _bounded_env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _get_ollama_config() -> dict:
    """Read Ollama config once per config-file revision."""
    try:
        from core.config import CONFIG_PATH, ConfigManager

        try:
            mtime_ns = CONFIG_PATH.stat().st_mtime_ns
        except OSError:
            mtime_ns = -1
        if _CONFIG_CACHE["mtime_ns"] == mtime_ns:
            return deepcopy(_CONFIG_CACHE["data"])
        with _CONFIG_CACHE_LOCK:
            if _CONFIG_CACHE["mtime_ns"] == mtime_ns:
                return deepcopy(_CONFIG_CACHE["data"])
            cfg = ConfigManager()
            data = cfg.config.get("ollama", {})
            if not isinstance(data, dict):
                data = {}
            _CONFIG_CACHE["mtime_ns"] = mtime_ns
            _CONFIG_CACHE["data"] = deepcopy(data)
            return deepcopy(data)
    except Exception:
        return {}


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_ollama_host(value: str) -> str:
    host = str(value or OLLAMA_BASE_URL).strip().rstrip("/")
    if host.endswith("/api"):
        host = host[:-4].rstrip("/")
    if host.endswith("/v1"):
        host = host[:-3].rstrip("/")
    return host or OLLAMA_BASE_URL


def _local_cloud_model_name(value: str) -> str:
    model = str(value or "").strip()
    if not model:
        return ""
    return model if model.endswith("-cloud") else f"{model}-cloud"


def _subscription_serves(model: Any) -> bool:
    try:
        from core.ai.subscription_routing import subscription_provider_for

        return subscription_provider_for(model) is not None
    except Exception:
        return False


def _subscription_generate(prompt: str, *, model: str, system: str, timeout_override: Any) -> Optional[Dict[str, Any]]:
    try:
        from core.ai.subscription_routing import generate_via_subscription
    except Exception:
        return None
    return generate_via_subscription(prompt, model=model, system=system, timeout_override=timeout_override)


def _subscription_fallback_model(model: Any) -> str:
    try:
        from core.ai.subscription_routing import fallback_model_for

        return fallback_model_for(model)
    except Exception:
        return ""


class OllamaClient:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = self._resolve_base_url(base_url)
        self._active_model = None
        self._availability_lock = threading.Lock()
        self._availability_cache: Dict[str, Any] = {
            "key": None,
            "checked_at": 0.0,
            "available": False,
        }
        self._models_lock = threading.Lock()
        self._models_cache: Dict[str, Any] = {
            "key": None,
            "checked_at": 0.0,
            "models": [],
        }
        self._cloud_models_lock = threading.Lock()
        self._cloud_models_cache: Dict[str, Any] = {
            "checked_at": 0.0,
            "models": [],
        }

    def _cache_identity(self) -> tuple:
        # Do not retain the API key itself in diagnostic or cache state.
        return self.base_url, bool(self.get_api_key())

    def _resolve_base_url(self, base_url: Optional[str] = None) -> str:
        cfg = _get_ollama_config()
        explicit_env_or_arg = base_url or os.getenv("OLLAMA_BASE_URL")
        configured = explicit_env_or_arg or cfg.get("base_url")
        api_key = self.get_api_key(config=cfg)
        use_cloud_auto = _truthy(cfg.get("use_cloud_when_api_key_present"), True)
        force_local = _truthy(cfg.get("force_local"), False)
        if not explicit_env_or_arg and api_key and use_cloud_auto and not force_local:
            configured = cfg.get("cloud_base_url") or "https://ollama.com"
        return _normalize_ollama_host(configured or OLLAMA_BASE_URL)

    def refresh_config(self) -> None:
        """Refresh base URL after config/env changes without recreating callers."""
        self.base_url = self._resolve_base_url(None)

    def is_cloud_host(self) -> bool:
        try:
            host = urlparse(self.base_url).netloc.lower()
            return host == "ollama.com" or host.endswith(".ollama.com")
        except Exception:
            return False

    def get_api_key(self, *, config: Optional[Dict[str, Any]] = None) -> str:
        cfg = config if isinstance(config, dict) else _get_ollama_config()
        env_name = str(cfg.get("api_key_env") or "OLLAMA_API_KEY").strip()
        direct_key = str(cfg.get("api_key") or "").strip()
        return str(os.getenv(env_name, "") or direct_key).strip()

    def _headers(self) -> Dict[str, str]:
        api_key = self.get_api_key()
        if not api_key:
            return {}
        return {"Authorization": f"Bearer {api_key}"}

    def _api_url(self, path: str) -> str:
        return f"{self.base_url}/api/{str(path or '').lstrip('/')}"

    def check_availability(self, *, force: bool = False) -> bool:
        """Check Ollama once per TTL and collapse concurrent probes into one request."""
        self.refresh_config()
        if self.is_cloud_host() and not self.get_api_key():
            return False
        key = self._cache_identity()
        now = time.monotonic()
        positive_ttl = _bounded_env_float("OLLAMA_AVAILABILITY_TTL_SECONDS", 30.0, 1.0, 300.0)
        negative_ttl = _bounded_env_float("OLLAMA_UNAVAILABLE_TTL_SECONDS", 5.0, 1.0, positive_ttl)
        cached_ttl = positive_ttl if self._availability_cache["available"] else negative_ttl
        if (
            not force
            and self._availability_cache["key"] == key
            and now - float(self._availability_cache["checked_at"]) < cached_ttl
        ):
            return bool(self._availability_cache["available"])

        with self._availability_lock:
            now = time.monotonic()
            cached_ttl = positive_ttl if self._availability_cache["available"] else negative_ttl
            if (
                not force
                and self._availability_cache["key"] == key
                and now - float(self._availability_cache["checked_at"]) < cached_ttl
            ):
                return bool(self._availability_cache["available"])
            available = False
            try:
                resp = requests.get(
                    self._api_url("tags"),
                    headers=self._headers(),
                    timeout=(3, 5),
                )
                available = resp.status_code == 200
                if available:
                    get_health_dashboard().register("ollama_availability", QualifiedValue.verified(True, "ollama"))
                    try:
                        from core.http_utils import _record_success
                        _record_success("ollama")
                    except Exception:
                        pass
                else:
                    get_health_dashboard().register(
                        "ollama_availability",
                        QualifiedValue.unavailable("ollama", f"HTTP {resp.status_code}"),
                    )
            except requests.RequestException as exc:
                get_health_dashboard().register("ollama_availability", QualifiedValue.unavailable("ollama", str(exc)))
            self._availability_cache = {
                "key": key,
                "checked_at": time.monotonic(),
                "available": available,
            }
            return available

    def get_models(self, *, force: bool = False) -> List[str]:
        """Return the model catalog from a bounded, thread-safe TTL cache."""
        self.refresh_config()
        if self.is_cloud_host() and not self.get_api_key():
            return []
        key = self._cache_identity()
        now = time.monotonic()
        positive_ttl = _bounded_env_float("OLLAMA_MODELS_TTL_SECONDS", 60.0, 1.0, 600.0)
        negative_ttl = _bounded_env_float("OLLAMA_MODELS_EMPTY_TTL_SECONDS", 10.0, 1.0, positive_ttl)
        cached_ttl = positive_ttl if self._models_cache["models"] else negative_ttl
        if (
            not force
            and self._models_cache["key"] == key
            and now - float(self._models_cache["checked_at"]) < cached_ttl
        ):
            return list(self._models_cache["models"])

        with self._models_lock:
            now = time.monotonic()
            cached_ttl = positive_ttl if self._models_cache["models"] else negative_ttl
            if (
                not force
                and self._models_cache["key"] == key
                and now - float(self._models_cache["checked_at"]) < cached_ttl
            ):
                return list(self._models_cache["models"])
            models: List[str] = []
            try:
                resp = http_get(
                    self._api_url("tags"),
                    provider="ollama",
                    timeout=(2, 5),
                    max_retries=1,
                    headers=self._headers(),
                )
                if resp is not None and resp.status_code == 200:
                    data = resp.json()
                    raw_models = data.get("models", []) if isinstance(data, dict) else []
                    models = [
                        str(model.get("name"))
                        for model in raw_models
                        if isinstance(model, dict) and str(model.get("name") or "").strip()
                    ]
                    get_health_dashboard().register("ollama_models", QualifiedValue.verified(len(models), "ollama"))
                elif resp is not None:
                    get_health_dashboard().register(
                        "ollama_models",
                        QualifiedValue.unavailable("ollama", f"HTTP {resp.status_code}"),
                    )
            except Exception as exc:
                logger.error(f"Error fetching Ollama models: {exc}")
                get_health_dashboard().register("ollama_models", QualifiedValue.unavailable("ollama", str(exc)))
            self._models_cache = {
                "key": key,
                "checked_at": time.monotonic(),
                "models": list(models),
            }
            return models

    @staticmethod
    def local_cloud_alias(model: str) -> str:
        """Translate a direct Ollama Cloud API id to its local proxy alias.

        ``https://ollama.com/api/tags`` returns direct-host ids such as
        ``glm-5.3-flash`` and ``gpt-oss:120b``.  The local Ollama daemon uses
        ``glm-5.3-flash:cloud`` and ``gpt-oss:120b-cloud`` respectively.
        Keeping this conversion in one place lets the same live catalog drive
        both direct-cloud and local-proxy deployments.
        """
        value = str(model or "").strip()
        if not value or value.endswith(":cloud") or value.endswith("-cloud"):
            return value
        return f"{value}-cloud" if ":" in value else f"{value}:cloud"

    def get_cloud_models(self, *, force: bool = False) -> List[str]:
        """Return the current public Ollama Cloud catalog with a TTL cache.

        Ollama documents ``GET https://ollama.com/api/tags`` as the canonical
        list of models available through its cloud API.  Catalog discovery is
        deliberately isolated from the runtime-provider circuit so an
        optional website outage cannot mark the working local daemon down.
        """
        now = time.monotonic()
        positive_ttl = _bounded_env_float(
            "OLLAMA_CLOUD_CATALOG_TTL_SECONDS", 900.0, 30.0, 86400.0
        )
        negative_ttl = _bounded_env_float(
            "OLLAMA_CLOUD_CATALOG_EMPTY_TTL_SECONDS", 60.0, 5.0, positive_ttl
        )
        cached = self._cloud_models_cache
        cached_ttl = positive_ttl if cached["models"] else negative_ttl
        if not force and now - float(cached["checked_at"]) < cached_ttl:
            return list(cached["models"])

        with self._cloud_models_lock:
            now = time.monotonic()
            cached = self._cloud_models_cache
            cached_ttl = positive_ttl if cached["models"] else negative_ttl
            if not force and now - float(cached["checked_at"]) < cached_ttl:
                return list(cached["models"])

            models: List[str] = []
            try:
                resp = http_get(
                    OLLAMA_CLOUD_CATALOG_URL,
                    provider="ollama_catalog",
                    timeout=(3, 10),
                    max_retries=1,
                )
                if resp is not None and resp.status_code == 200:
                    data = resp.json()
                    rows = data.get("models", []) if isinstance(data, dict) else []
                    seen = set()
                    for row in rows:
                        name = str(row.get("name") or row.get("model") or "").strip() \
                            if isinstance(row, dict) else ""
                        if name and name not in seen:
                            seen.add(name)
                            models.append(name)
                    get_health_dashboard().register(
                        "ollama_cloud_catalog",
                        QualifiedValue.verified(len(models), "ollama_catalog"),
                    )
                elif resp is not None:
                    get_health_dashboard().register(
                        "ollama_cloud_catalog",
                        QualifiedValue.unavailable(
                            "ollama_catalog", f"HTTP {resp.status_code}"
                        ),
                    )
            except Exception as exc:
                logger.warning("Ollama Cloud catalog discovery failed: %s", exc)
                get_health_dashboard().register(
                    "ollama_cloud_catalog",
                    QualifiedValue.unavailable("ollama_catalog", str(exc)),
                )

            self._cloud_models_cache = {
                "checked_at": time.monotonic(),
                "models": list(models),
            }
            return models

    def get_selectable_cloud_models(self, *, force: bool = False) -> List[str]:
        """Return cloud model ids compatible with the configured Ollama host."""
        models = self.get_cloud_models(force=force)
        if self.is_cloud_host():
            return models
        return [self.local_cloud_alias(model) for model in models]

    def pull_model(self, model: str, *, timeout: int = 180) -> Dict[str, Any]:
        """Activate a catalog model in the local daemon before selecting it.

        Cloud aliases only add a lightweight local manifest; model inference
        remains remote.  The call is explicit and non-streaming so the settings
        endpoint can report a real success/failure instead of saving a model
        that the runtime would silently ignore.
        """
        name = str(model or "").strip()
        if not name:
            return {"ok": False, "error": "Model name is required"}
        try:
            resp = http_post(
                self._api_url("pull"),
                provider="ollama",
                json={"model": name, "stream": False},
                headers=self._headers(),
                timeout=(5, max(30, int(timeout))),
                max_retries=0,
                priority=True,
                bypass_circuit=True,
            )
            if resp is None:
                return {"ok": False, "error": "Ollama pull circuit open"}
            if resp.status_code != 200:
                detail = str(getattr(resp, "text", "") or "").strip()[:500]
                return {
                    "ok": False,
                    "error": f"Ollama pull failed: HTTP {resp.status_code} {detail}".strip(),
                }
            with self._models_lock:
                self._models_cache = {
                    "key": None,
                    "checked_at": 0.0,
                    "models": [],
                }
            return {"ok": True, "model": name}
        except Exception as exc:
            logger.warning("Ollama model activation failed for %s: %s", name, exc)
            return {"ok": False, "error": str(exc)}

    def get_preferred_model(self) -> str:
        """Return model name from config, falling back to DEFAULT_OLLAMA_MODEL."""
        cfg = _get_ollama_config()
        if self.is_cloud_host():
            return cfg.get("cloud_model") or cfg.get("model") or "gpt-oss:120b"
        return cfg.get("model") or DEFAULT_OLLAMA_MODEL

    def get_timeout(self) -> int:
        """Return generation timeout from config."""
        cfg = _get_ollama_config()
        try:
            return int(cfg.get("timeout", DEFAULT_TIMEOUT))
        except (ValueError, TypeError):
            return DEFAULT_TIMEOUT

    def _resolve_read_timeout(self, timeout_override: Any = None) -> int:
        """Read timeout in whole seconds: a positive override wins, anything else
        (None, 0, negative, NaN, a non-number) falls back to the configured
        timeout. Never below 1 second."""
        seconds: Optional[float] = None
        if timeout_override is not None and not isinstance(timeout_override, bool):
            try:
                seconds = float(timeout_override)
            except (TypeError, ValueError):
                seconds = None
        if seconds is None or seconds != seconds or seconds <= 0 or seconds == float("inf"):
            seconds = float(self.get_timeout())
        if seconds != seconds or seconds <= 0 or seconds == float("inf"):
            seconds = float(DEFAULT_TIMEOUT)
        return max(1, int(round(seconds)))

    def get_best_model(self) -> Optional[str]:
        """
        Return preferred model if available, else first available model.
        """
        cfg = _get_ollama_config()
        preferred = self.get_preferred_model()
        # A Claude model served by the owner's subscription is not in Ollama's
        # list; it is still the selected model (see core.ai.subscription_routing).
        if _subscription_serves(preferred):
            return preferred
        models = self.get_models()
        if not models:
            return None

        # Exact match
        if preferred in models:
            return preferred

        if not self.is_cloud_host() and _truthy(cfg.get("use_local_cloud_model_fallback"), True):
            local_cloud_model = str(cfg.get("local_cloud_model") or "").strip()
            if not local_cloud_model:
                local_cloud_model = _local_cloud_model_name(cfg.get("cloud_model") or "")
            if local_cloud_model and local_cloud_model in models:
                logger.info(
                    "Preferred Ollama model %s is unavailable; using local cloud model %s",
                    preferred,
                    local_cloud_model,
                )
                return local_cloud_model

        # Partial match (e.g. "gemma3:12b" matches "gemma3:12b-instruct")
        for m in models:
            if m.startswith(preferred.split(":")[0]):
                return m

        # Fallback: first available model
        return models[0] if models else None

    def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        system: str = "",
        ui_call: bool = False,
        timeout_override: Optional[int] = None,
        manual_request: bool = False,
    ) -> Dict[str, Any]:
        """
        Generate text response.

        Parameters
        ----------
        ui_call : bool
            When True, caps the read timeout to UI_TIMEOUT_CAP (15s) so
            the UI is never blocked by a slow LLM. Background agents
            should pass ui_call=False (default) to use the full budget.

        Returns dict with 'response', 'model', 'done', and
        '_meta' with model, generated_at, ui_call.
        """
        if not model:
            model = self.get_best_model()

        if not model:
            raise ValueError(f"No Ollama models available. Run 'ollama pull {self.get_preferred_model()}'.")

        # Claude models run on the owner's Claude subscription. When it cannot
        # answer (limit reached, CLI missing, daily cap) the call continues on
        # the configured Ollama fallback model, and the result says so.
        requested_model, subscription_note = model, ""
        routed = _subscription_generate(
            prompt, model=model, system=system,
            timeout_override=(min(self._resolve_read_timeout(timeout_override), UI_TIMEOUT_CAP)
                              if ui_call else timeout_override),
        )
        if routed is not None:
            if not routed.get("error"):
                routed.setdefault("_meta", {}).update({"ui_call": ui_call, "manual_request": manual_request})
                return routed
            fallback = _subscription_fallback_model(model)
            if not fallback:
                return {"error": str(routed["error"])}
            subscription_note = str(routed["error"])
            logger.warning("Claude subscription could not serve %s (%s); using %s", model, subscription_note[:160], fallback)
            model = fallback

        base_timeout = self._resolve_read_timeout(timeout_override)
        if ui_call:
            read_timeout = min(base_timeout, UI_TIMEOUT_CAP)
        else:
            read_timeout = base_timeout

        # Use the /api/chat endpoint (not /api/generate): ollama.com's CLOUD API
        # does NOT expose /api/generate (HTTP 404), only /api/chat. Local Ollama
        # supports both, so /api/chat is the universally-compatible choice.
        messages = []
        if str(system or "").strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }

        try:
            self.refresh_config()
            resp = http_post(
                self._api_url("chat"),
                provider="ollama",
                json=payload,
                headers=self._headers(),
                timeout=(5, read_timeout),
                max_retries=0,
                priority=manual_request,
                bypass_circuit=manual_request,
            )
            if resp is None:
                get_health_dashboard().register("ollama_generate", QualifiedValue.unavailable("ollama", "circuit open"))
                try:
                    from core.observability import record_external_error
                    record_external_error(source="ollama", error="circuit open")
                except Exception:
                    pass
                return {"error": "Ollama circuit open"}
            if resp.status_code == 200:
                data = resp.json()
                get_health_dashboard().register("ollama_generate", QualifiedValue.verified(model, "ollama"))
                # /api/chat returns the text under message.content; every existing
                # caller reads `response` (the /api/generate field). Normalize so
                # neither the callers nor the JSON-extraction helpers need to know
                # which endpoint was used.
                msg_content = ""
                try:
                    msg_content = str((data.get("message") or {}).get("content") or "")
                except Exception:
                    msg_content = ""
                if msg_content and not data.get("response"):
                    data["response"] = msg_content
                # Attach metadata for determinism tracking
                from datetime import datetime, timezone
                data.setdefault("_meta", {})
                data["_meta"].update({
                    "model": model,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "ui_call": ui_call,
                    "manual_request": manual_request,
                    "timeout_sec": read_timeout,
                })
                if subscription_note:
                    data["_meta"].update({
                        "requested_model": requested_model,
                        "subscription_fallback_reason": subscription_note[:300],
                    })
                return data
            else:
                get_health_dashboard().register("ollama_generate", QualifiedValue.unavailable("ollama", f"HTTP {resp.status_code}"))
                logger.error(f"Ollama generation failed: {resp.text}")
                try:
                    from core.observability import record_external_error
                    record_external_error(source="ollama", error=f"HTTP {resp.status_code}")
                except Exception:
                    pass
                return {"error": f"Ollama error: {resp.status_code}"}
        except Exception as exc:
            get_health_dashboard().register("ollama_generate", QualifiedValue.unavailable("ollama", str(exc)))
            logger.error(f"Ollama connection error: {exc}")
            try:
                from core.observability import record_external_error
                record_external_error(source="ollama", error=str(exc))
            except Exception:
                pass
            return {"error": str(exc)}

# Global instance
ollama = OllamaClient()
