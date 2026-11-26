"""§54 'use Qlib where appropriate' — import-guarded ADAPTER (Phase 2 manifest:
Qlib is ADAPTER, V9 data model authoritative). Optional; zero new deps (§118).
Owner installs qlib to activate; until then this is an honest stub (§4)."""
from __future__ import annotations
from typing import Any, Dict


def is_available() -> bool:
    try:
        import qlib  # noqa: F401
    except Exception:
        return False
    return True


def adapt(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Adapt Qlib to the V9 data model. If qlib is not installed, return an
    honest unavailable result (§4) — never fake."""
    if not is_available():
        return {"available": False, "reason": "qlib_not_installed_owner_opt_in"}
    return {"available": True, "reason": "qlib_adapter_pending_owner_activation",
            "note": "V9 data model authoritative; Qlib used as ADAPTER only"}