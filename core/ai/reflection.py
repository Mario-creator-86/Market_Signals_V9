"""Post-outcome reflection lessons (adopted from TradingAgents Phase-B reflector).

The decision-quality loop records realized outcomes (`position_close`). This
module closes the last gap: turning each closed pre-earnings decision into a
2-4 sentence lesson via ONE short LLM call, storing it in
``data/ai_reflections.jsonl`` (decision_id-keyed, append-only), and injecting
the latest lessons for the strategy family into the §41 evidence context so
future reviews are informed by what actually happened — not just scores.

Boundaries (owner spec): evidence-only, never a veto, fail-open everywhere;
one call per closed position, pre-earnings scope by default.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import DATA_DIR
from core.prompt_evidence import UNTRUSTED_NOTICE, clean_text, evidence_block

logger = logging.getLogger("AgentManager")

REFLECTIONS_PATH = DATA_DIR / "ai_reflections.jsonl"
_REFLECTION_PROMPT_VERSION = "position_reflection_v1"
_MAX_LESSONS_IN_PROMPT = 3
_MAX_LINE_CHARS = 600
# A reflection is written while a position is being closed: it may not hold
# that path for a provider's full default timeout.
_REFLECTION_TIMEOUT_SECONDS = 30.0
_write_lock = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reflection_prompt(record: Dict[str, Any]) -> str:
    return (
        "You are a trading analyst reviewing your own past decision now that the outcome is known.\n"
        "Write exactly 2-4 sentences of plain prose (no bullets, no headers, no markdown).\n\n"
        "Cover in order:\n"
        "1. Was the directional call correct (cite the realized P&L)?\n"
        "2. Which part of the thesis held or failed (gap, news, sizing)?\n"
        "3. One concrete lesson for the next similar pre-earnings setup.\n\n"
        "Be specific and terse; every word must earn its place.\n"
        f"{UNTRUSTED_NOTICE}\n\n"
        f"DECISION:\n{evidence_block('decision_summary', [record.get('decision_summary')], limit=1, max_chars=600)}\n"
        f"OUTCOME: realized {record.get('pnl_pct'):+.2f}% "
        f"(exit reason: {clean_text(record.get('close_reason'), max_chars=120)})"
    )


def reflection_enabled(config: Any = None) -> bool:
    try:
        cfg = getattr(config, "config", config) or {}
        ollama_cfg = (cfg.get("ollama") or {}) if isinstance(cfg, dict) else {}
        return bool(ollama_cfg.get("reflection_enabled", True))
    except Exception:
        return True


def load_lessons(strategy_family: str, limit: int = _MAX_LESSONS_IN_PROMPT) -> List[Dict[str, Any]]:
    """Latest lessons for one strategy family (bounded, fail-open)."""
    try:
        if not REFLECTIONS_PATH.exists():
            return []
        family = str(strategy_family or "").upper()
        rows: List[Dict[str, Any]] = []
        with REFLECTIONS_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if str(row.get("strategy_family") or "").upper() == family and row.get("lesson"):
                    rows.append(row)
        return rows[-limit:]
    except Exception:
        return []


def reflect_on_closed_position(
    position: Dict[str, Any],
    *,
    decision_summary: str,
    pnl_pct: float,
    close_reason: str,
    config: Any = None,
) -> Optional[Dict[str, Any]]:
    """One bounded LLM reflection for a closed position; appends to the store.

    Returns the stored reflection row, or None when unavailable/duplicate."""
    if not reflection_enabled(config):
        return None
    try:
        from core.ai.decision_engine import _build_providers

        cfg = getattr(config, "config", config) or {}
        provider, model, _ = _build_providers(cfg, {}, role="market_analyst")
        if provider is None:
            return None
        strategy_type = str(position.get("strategy_type") or "")
        family = strategy_type.split("_")[0] if strategy_type else ""
        decision_id = str((position.get("decision_context") or {}).get("decision_id") or "")
        if not decision_id:
            return None
        if _has_reflection(decision_id):
            return None

        record = {
            "decision_id": decision_id,
            "ticker": position.get("ticker"),
            "strategy_type": strategy_type,
            "strategy_family": family,
            "decision_summary": decision_summary[:600],
            "pnl_pct": round(float(pnl_pct), 3),
            "close_reason": close_reason,
            "reflected_at": _utc_now_iso(),
            "prompt_version": _REFLECTION_PROMPT_VERSION,
        }
        resp = provider.generate(
            _reflection_prompt(record),
            model=model,
            system="You write terse post-trade lessons. Plain prose only.",
            timeout_override=_REFLECTION_TIMEOUT_SECONDS,
        )
        if getattr(resp, "is_error", False):
            return None
        # The lesson is fed back into later review prompts: store one clean line.
        lesson = clean_text(getattr(resp, "text", ""), max_chars=_MAX_LINE_CHARS)
        if not lesson:
            return None
        record["lesson"] = lesson
        record["model"] = str(model or getattr(provider, "name", "ai"))
        _append_row(record)
        return record
    except Exception as exc:
        logger.debug(f"reflection skipped: {type(exc).__name__}: {str(exc)[:120]}")
        return None


def _has_reflection(decision_id: str) -> bool:
    try:
        if not REFLECTIONS_PATH.exists():
            return False
        with REFLECTIONS_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                if f'"decision_id": "{decision_id}"' in line:
                    return True
        return False
    except Exception:
        return False


def _append_row(record: Dict[str, Any]) -> None:
    with _write_lock:
        REFLECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with REFLECTIONS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


__all__ = [
    "reflect_on_closed_position", "load_lessons", "reflection_enabled",
    "REFLECTIONS_PATH",
]
