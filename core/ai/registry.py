"""ModelRegistry (spec §36) — the catalog of AI models across all providers.

§36 fields: provider, model_id, display_name, capabilities, tool_support,
structured_output_support, context_metadata, health, enabled, last_checked.
Discover dynamically when the provider supports it (list_models); allow manual
configuration otherwise. Persisted to config["ai"]["registry"] as a list of dicts.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ModelEntry:
    provider: str
    model_id: str
    display_name: str
    capabilities: Tuple[str, ...] = ("text",)
    tool_support: bool = True
    structured_output_support: bool = True
    context_metadata: Dict[str, Any] = field(default_factory=dict)
    health: str = "unknown"  # "unknown" | "healthy" | "unhealthy"
    enabled: bool = True
    last_checked: Optional[str] = None  # ISO timestamp


class ModelRegistry:
    def __init__(self) -> None:
        self._entries: Dict[str, ModelEntry] = {}  # keyed by model_id

    def register(self, entry: ModelEntry) -> None:
        self._entries[entry.model_id] = entry

    def get(self, model_id: str) -> Optional[ModelEntry]:
        return self._entries.get(model_id)

    def all_entries(self) -> List[ModelEntry]:
        return list(self._entries.values())

    def enabled_entries(self) -> List[ModelEntry]:
        return [e for e in self._entries.values() if e.enabled]

    def update_health(self, model_id: str, *, ok: bool) -> None:
        e = self._entries.get(model_id)
        if e is None:
            return
        self._entries[model_id] = ModelEntry(**{**asdict(e), "health": "healthy" if ok else "unhealthy"})

    def discover(self, provider: Any) -> List[ModelEntry]:
        """Populate the registry from a provider's list_models(). Returns newly added entries."""
        added: List[ModelEntry] = []
        try:
            caps = provider.capabilities()
        except Exception:
            caps = None
        for mid in provider.list_models() or []:
            if mid in self._entries:
                continue
            entry = ModelEntry(
                provider=getattr(provider, "name", "unknown"),
                model_id=mid,
                display_name=mid,
                capabilities=("text", "structured") if (caps and caps.structured_output) else ("text",),
                tool_support=bool(caps and caps.tool_calling) if caps else True,
                structured_output_support=bool(caps and caps.structured_output) if caps else True,
                context_metadata={},
                health="unknown",
                enabled=True,
                last_checked=None,
            )
            self._entries[mid] = entry
            added.append(entry)
        return added

    def to_list(self) -> List[Dict[str, Any]]:
        return [asdict(e) for e in self._entries.values()]

    def from_list(self, rows: List[Dict[str, Any]]) -> None:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            mid = row.get("model_id")
            if not mid:
                continue
            caps = row.get("capabilities", ("text",))
            if isinstance(caps, list):
                caps = tuple(caps)
            self._entries[mid] = ModelEntry(
                provider=str(row.get("provider", "")),
                model_id=str(mid),
                display_name=str(row.get("display_name", mid)),
                capabilities=tuple(caps) if isinstance(caps, (list, tuple)) else ("text",),
                tool_support=bool(row.get("tool_support", True)),
                structured_output_support=bool(row.get("structured_output_support", True)),
                context_metadata=dict(row.get("context_metadata") or {}),
                health=str(row.get("health", "unknown")),
                enabled=bool(row.get("enabled", True)),
                last_checked=row.get("last_checked"),
            )