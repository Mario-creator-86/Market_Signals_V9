"""Claude through the owner's Claude subscription (Claude Code CLI, headless).

The other providers call an HTTP API with a key. A Claude subscription has no
API key: the supported programmatic route is the Claude Code CLI in print mode
(`claude -p`), which signs in with the login stored by `claude auth login`.
This provider runs one prompt per call as plain text in, text out:

- no tools, no MCP servers, no skills, no user or project settings, and an
  empty working directory, so the model can only answer with text and cannot
  see or touch the project;
- the prompt travels over stdin as UTF-8, never on the command line;
- `ANTHROPIC_API_KEY` and friends are removed from the child environment,
  because the CLI would otherwise bill the API key instead of the subscription.

Usage counts against the subscription's own limits. When the limit is reached
the CLI reports an error, which comes back as an error response so the caller's
fallback chain can take over.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from core.ai.provider import (
    AIProvider,
    AIResponse,
    AIStreamChunk,
    ProviderCapabilities,
    Usage,
    call_timeout,
)
from core.bounded_process import BoundedProcessTimeout, run_bounded

logger = logging.getLogger(__name__)

PROVIDER_TYPE = "claude_code"
DEFAULT_TIMEOUT_SECONDS = 300
# Variables that make the CLI use an API key or another endpoint instead of the
# subscription login.
_AUTH_OVERRIDE_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
_MODEL_ALIASES = ("opus", "sonnet", "haiku", "fable")
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# Windows caps a command line near 32k characters; a longer system prompt is
# sent at the top of the stdin prompt instead.
_MAX_SYSTEM_ARG_CHARS = 8000


_VERSION_CACHE: Dict[str, str] = {}


def _version_tuple(version: str) -> tuple:
    parts = []
    for piece in str(version or "").strip().split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def newest_cli(candidates: List[tuple]) -> str:
    """Path with the highest version from `(path, version)` pairs ("" when none)."""
    best = ""
    best_version: tuple = ()
    for path, version in candidates:
        if not path:
            continue
        current = _version_tuple(version)
        if not best or current > best_version:
            best, best_version = str(path), current
    return best


def _cli_version(path: str) -> str:
    """`claude --version` for one executable, cached per path and file time."""
    try:
        key = f"{path}|{Path(path).stat().st_mtime_ns}"
    except OSError:
        return ""
    if key not in _VERSION_CACHE:
        try:
            done = run_bounded([path, "--version"], timeout=20)
            _VERSION_CACHE[key] = (done.stdout or "").strip().split(" ")[0] if done.returncode == 0 else ""
        except Exception:
            _VERSION_CACHE[key] = ""
    return _VERSION_CACHE[key]


def installed_clis() -> List[tuple]:
    """`(path, version)` for every Claude Code CLI found: PATH, ~/.local/bin and
    the copies bundled with the Claude desktop app (version = folder name)."""
    suffix = ".exe" if os.name == "nt" else ""
    found: Dict[str, str] = {}
    for candidate in (shutil.which("claude"), str(Path.home() / ".local" / "bin" / f"claude{suffix}")):
        if candidate and Path(candidate).exists() and str(candidate) not in found:
            found[str(candidate)] = _cli_version(str(candidate))
    appdata = os.environ.get("APPDATA") or ""
    bundle_root = Path(appdata) / "Claude" / "claude-code" if appdata else None
    if bundle_root is not None and bundle_root.is_dir():
        try:
            for version_dir in bundle_root.iterdir():
                if not version_dir.is_dir():
                    continue
                for exe in version_dir.glob(f"*/claude{suffix}"):
                    found[str(exe)] = version_dir.name
        except OSError:
            pass
    return list(found.items())


def _extract_json_object(text: str) -> Optional[Any]:
    body = str(text or "").strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(body[start:end + 1])
    except (ValueError, TypeError):
        return None


class ClaudeCodeProvider(AIProvider):
    name = "claude_subscription"

    def __init__(
        self,
        *,
        name: str = "claude_subscription",
        executable: str = "",
        default_model: str = "",
        models: Optional[List[str]] = None,
        effort: str = "",
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        max_concurrent: int = 2,
        runner: Optional[Callable[..., Any]] = None,
        workdir: Optional[Path] = None,
    ) -> None:
        self.name = name
        self.executable = str(executable or "").strip()
        self.default_model = str(default_model or "").strip()
        self.models = [str(m).strip() for m in (models or []) if str(m).strip()]
        effort = str(effort or "").strip().lower()
        self.effort = effort if effort in _EFFORT_LEVELS else ""
        self.timeout = int(timeout or DEFAULT_TIMEOUT_SECONDS)
        self._runner = runner or run_bounded
        self._workdir = Path(workdir) if workdir else Path(tempfile.gettempdir()) / "marketmuse_claude_cli"
        self._lock = threading.Lock()
        # Each call is a CLI process of a few hundred MB: never more than a few at once.
        self.max_concurrent = max(1, int(max_concurrent or 1))
        self._slots = threading.BoundedSemaphore(self.max_concurrent)
        self._last_usage: Usage = Usage()

    # --- discovery -----------------------------------------------------------
    def resolve_executable(self) -> str:
        """The configured CLI, else the newest one installed on this machine.

        A newer model needs a newer CLI ("Claude Code 2.1.289 does not support
        this model"), and the copy on PATH can lag behind the one bundled with
        the Claude desktop app, so the newest installed version is used.
        """
        if self.executable:
            return self.executable
        return newest_cli(installed_clis())

    def serves_model(self, model: Any) -> bool:
        """True for Claude model ids and aliases; this provider serves nothing else."""
        value = str(model or "").strip().lower()
        return bool(value) and (value.startswith("claude-") or value in _MODEL_ALIASES)

    def list_models(self) -> List[str]:
        if self.models:
            return list(self.models)
        return [self.default_model] if self.default_model else []

    def health_check(self, *, force: bool = False) -> bool:
        """The CLI is installed. Sign-in and quota only show on a real call."""
        path = self.resolve_executable()
        return bool(path) and Path(path).exists()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            text=True, structured_output=True, tool_calling=False, streaming=False,
            supported_formats=("json",),
        )

    # --- generation ----------------------------------------------------------
    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        system: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        **opts: Any,
    ) -> AIResponse:
        executable = self.resolve_executable()
        if not executable:
            return AIResponse(text="", error="Claude CLI not found (install Claude Code and run 'claude auth login')")
        chosen = str(model or self.default_model or "").strip()
        if not chosen:
            return AIResponse(text="", error="no Claude model configured for the subscription provider")

        system_text = str(system or "").strip()
        body = str(prompt or "")
        cmd = [
            executable, "-p", "--model", chosen, "--output-format", "json",
            "--tools", "", "--setting-sources", "", "--strict-mcp-config",
            "--disable-slash-commands", "--no-session-persistence",
        ]
        if self.effort:
            cmd += ["--effort", self.effort]
        if system_text and len(system_text) <= _MAX_SYSTEM_ARG_CHARS:
            cmd += ["--system-prompt", system_text]
        elif system_text:
            body = f"{system_text}\n\n{body}"

        env = {k: v for k, v in os.environ.items() if k not in _AUTH_OVERRIDE_ENV}
        deadline = call_timeout(self.timeout, opts)
        if not self._slots.acquire(timeout=float(deadline)):
            return AIResponse(text="", error=f"Claude subscription busy: {self.max_concurrent} calls already running")
        try:
            self._workdir.mkdir(parents=True, exist_ok=True)
            completed = self._runner(
                cmd, input_text=body, timeout=deadline, cwd=str(self._workdir), env=env,
            )
        except BoundedProcessTimeout:
            return AIResponse(text="", error=f"Claude CLI timed out after {float(deadline):.0f}s")
        except OSError as exc:
            return AIResponse(text="", error=f"Claude CLI could not start: {exc.__class__.__name__}")
        finally:
            self._slots.release()

        payload = self._parse_result(getattr(completed, "stdout", "") or "")
        if payload is None:
            detail = (getattr(completed, "stderr", "") or getattr(completed, "stdout", "") or "").strip()
            return AIResponse(
                text="",
                error=f"Claude CLI exit {getattr(completed, 'returncode', '?')}: {detail[:300] or 'no output'}",
            )
        usage = self._usage_from(payload)
        with self._lock:
            self._last_usage = usage
        text = str(payload.get("result") or "")
        if payload.get("is_error") or str(payload.get("subtype") or "success") != "success":
            reason = text.strip() or str(payload.get("subtype") or "error")
            return AIResponse(text="", error=f"Claude CLI error: {reason[:300]}", usage=usage)
        served = next(iter(payload.get("modelUsage") or {}), "") if isinstance(payload.get("modelUsage"), dict) else ""
        return AIResponse(
            text=text, usage=usage, model=str(served or chosen),
            raw={"stop_reason": payload.get("stop_reason"), "duration_ms": payload.get("duration_ms"),
                 "num_turns": payload.get("num_turns")},
        )

    def generate_structured(
        self,
        prompt: str,
        schema: Dict[str, Any],
        *,
        model: Optional[str] = None,
        system: str = "",
        **opts: Any,
    ) -> AIResponse:
        schema_hint = json.dumps(schema) if schema else ""
        instruction = (
            f"{prompt}\n\nRespond with a SINGLE JSON object matching this schema "
            f"(extra keys allowed, no prose):\n{schema_hint}"
        )
        resp = self.generate(instruction, model=model, system=system, **opts)
        if resp.is_error:
            return resp
        parsed = _extract_json_object(resp.text)
        if parsed is None:
            return AIResponse(text=resp.text, error="structured output parse failed",
                              usage=resp.usage, model=resp.model, raw={"json": None})
        return AIResponse(text=resp.text, usage=resp.usage, model=resp.model, raw={"json": parsed})

    def tool_call(
        self,
        prompt: str,
        tools: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        system: str = "",
        **opts: Any,
    ) -> AIResponse:
        return AIResponse(text="", error="tool calls are not available through the Claude subscription provider")

    def stream(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        system: str = "",
        **opts: Any,
    ) -> Iterator[AIStreamChunk]:
        resp = self.generate(prompt, model=model, system=system, **opts)
        yield AIStreamChunk(text=resp.text, done=True)

    def usage(self) -> Usage:
        with self._lock:
            return self._last_usage

    # --- internals -----------------------------------------------------------
    @staticmethod
    def _parse_result(stdout: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(str(stdout or "").strip())
        except (ValueError, TypeError):
            return None
        if isinstance(data, list):  # stream-style array: the result event is last
            data = next((item for item in reversed(data)
                         if isinstance(item, dict) and item.get("type") == "result"), None)
        if not isinstance(data, dict) or "result" not in data and "is_error" not in data:
            return None
        return data

    @staticmethod
    def _usage_from(payload: Dict[str, Any]) -> Usage:
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        prompt, completion = usage.get("input_tokens"), usage.get("output_tokens")
        try:
            prompt = int(prompt) if prompt is not None else None
            completion = int(completion) if completion is not None else None
        except (TypeError, ValueError):
            prompt = completion = None
        total = prompt + completion if prompt is not None and completion is not None else None
        return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


__all__ = ["ClaudeCodeProvider", "PROVIDER_TYPE", "installed_clis", "newest_cli"]
