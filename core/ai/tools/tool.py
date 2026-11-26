"""§39 Tool + ToolResult value types.

A Tool is the unit the AI may call. The registry is the only surface the AI
reaches (§40): no shell/Python/SQL/filesystem/HTTP/creds/broker tool is ever
constructed. Each tool wraps an existing read-only service function (§5 —
evolve, not rewrite); `propose_order`/`propose_close` return drafts only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    arguments_schema: Dict[str, Any]
    execute: Callable[..., ToolResult]