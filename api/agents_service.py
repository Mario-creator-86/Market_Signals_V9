"""Business logic for the bounded Agents control plane.

FastAPI route handlers stay in ``api.main``; this module owns the control-plane
shape and the invariant that execution/risk cannot be disabled while the bot is
running.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from functools import lru_cache
from numbers import Real
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from core.portfolio.constraints import RiskConstitution, RiskConstitutionError
from core.risk_limits import (
    MAX_DAILY_LOSS_PCT,
    MAX_RISK_PER_TRADE_PCT,
    MIN_DAILY_LOSS_PCT,
    MIN_RISK_PER_TRADE_PCT,
    validate_risk_limits,
)


logger = logging.getLogger(__name__)


CONTROL_PLANE_AGENTS = frozenset(
    {
        "execution",
        "risk",
        "scanner",
        "sentiment",
        "news_tab",
        "economic_calendar",
        "trade_radar",
        "strategy_research",
        "premarket_scanner",
        "pre_earnings",
        "research_factory",
        "coding_agent",
        "critic_agent",
        "ui_inspector",
    }
)
REQUIRED_WHILE_RUNNING = frozenset({"execution", "risk"})


RISK_CONSTITUTION_SCHEMA = (
    {
        "name": "max_risk_per_trade_pct",
        "label": "Max risk / trade",
        "section": "Position & portfolio",
        "kind": "number",
        "unit": "%",
        "minimum": MIN_RISK_PER_TRADE_PCT,
        "maximum": MAX_RISK_PER_TRADE_PCT,
        "step": 0.05,
        "help": "Maximum equity loss at the stop, after leverage.",
    },
    {
        "name": "max_position_size_pct",
        "label": "Max position size",
        "section": "Position & portfolio",
        "kind": "number",
        "unit": "%",
        "minimum": 0.0,
        "maximum": 100.0,
        "step": 0.05,
        "help": "Maximum capital allocated to one symbol.",
    },
    {
        "name": "max_total_exposure_pct",
        "label": "Max total exposure",
        "section": "Position & portfolio",
        "kind": "number",
        "unit": "%",
        "minimum": 0.0,
        "maximum": 100.0,
        "step": 0.5,
        "help": "Maximum combined notional exposure across open positions.",
    },
    {
        "name": "max_leverage",
        "label": "Max leverage",
        "section": "Position & portfolio",
        "kind": "integer",
        "unit": "×",
        "minimum": 1,
        "maximum": 5,
        "step": 1,
        "help": "Hard leverage ceiling supported by paper execution.",
    },
    {
        "name": "minimum_cash_pct",
        "label": "Minimum cash reserve",
        "section": "Position & portfolio",
        "kind": "number",
        "unit": "%",
        "minimum": 0.0,
        "maximum": 100.0,
        "step": 0.5,
        "help": "Equity that must remain available after a new entry.",
    },
    {
        "name": "max_positions",
        "label": "Max open positions",
        "section": "Position & portfolio",
        "kind": "integer",
        "unit": "positions",
        "minimum": 1,
        "maximum": 100,
        "step": 1,
        "help": "Maximum number of simultaneously open positions.",
    },
    {
        "name": "max_sector_exposure_pct",
        "label": "Max sector exposure",
        "section": "Concentration & liquidity",
        "kind": "number",
        "unit": "%",
        "minimum": 0.0,
        "maximum": 100.0,
        "step": 0.5,
        "help": "Maximum notional exposure assigned to one sector.",
    },
    {
        "name": "correlation_threshold",
        "label": "Correlation threshold",
        "section": "Concentration & liquidity",
        "kind": "number",
        "unit": "ratio",
        "minimum": 0.0,
        "maximum": 1.0,
        "step": 0.01,
        "help": "Absolute correlation from which positions are grouped.",
    },
    {
        "name": "max_correlated_exposure_pct",
        "label": "Max correlated exposure",
        "section": "Concentration & liquidity",
        "kind": "number",
        "unit": "%",
        "minimum": 0.0,
        "maximum": 100.0,
        "step": 0.5,
        "help": "Maximum combined exposure in the correlated group.",
    },
    {
        "name": "max_liquidity_participation_pct",
        "label": "Max liquidity participation",
        "section": "Concentration & liquidity",
        "kind": "number",
        "unit": "% ADV",
        "minimum": 0.0,
        "maximum": 100.0,
        "step": 0.01,
        "help": "Maximum order notional as a share of average daily volume.",
    },
    {
        "name": "max_daily_loss_pct",
        "label": "Max daily loss",
        "section": "Loss controls",
        "kind": "number",
        "unit": "%",
        "minimum": MIN_DAILY_LOSS_PCT,
        "maximum": MAX_DAILY_LOSS_PCT,
        "step": 0.1,
        "help": "Daily realized-loss breaker measured against equity.",
    },
    {
        "name": "max_daily_loss_trades",
        "label": "Max losing trades / day",
        "section": "Loss controls",
        "kind": "integer",
        "unit": "trades",
        "minimum": 1,
        "maximum": 100,
        "step": 1,
        "help": "Maximum daily closed losing trades before blocking new entries.",
    },
    {
        "name": "max_drawdown_pct",
        "label": "Max drawdown",
        "section": "Loss controls",
        "kind": "number",
        "unit": "%",
        "minimum": 0.0,
        "maximum": 100.0,
        "step": 0.1,
        "help": "Maximum peak-to-current equity drawdown.",
    },
    {
        "name": "max_data_age_seconds",
        "label": "Max market-data age",
        "section": "Data & governance",
        "kind": "integer",
        "unit": "seconds",
        "minimum": 1,
        "step": 1,
        "help": "Oldest evidence accepted for a new entry.",
    },
    {
        "name": "block_data_conflicts",
        "label": "Block conflicting data",
        "section": "Data & governance",
        "kind": "boolean",
        "unit": "",
        "help": "Veto entries when authoritative sources disagree.",
    },
    {
        "name": "require_approved_strategy",
        "label": "Require approved strategy",
        "section": "Data & governance",
        "kind": "boolean",
        "unit": "",
        "help": "Allow entries only from the approved strategy registry.",
    },
)
RISK_CONSTITUTION_FIELD_NAMES = tuple(
    field["name"] for field in RISK_CONSTITUTION_SCHEMA
)

# Risk Manager is the sole owner of settings that can admit, size, or
# automatically exit a paper trade.  ``path``/``mirror_paths`` are internal
# compatibility adapters for workers that still consume the established config
# layout; the public API exposes the logical policy names only.
RISK_TRADE_POLICY_SCHEMA = (
    {
        "name": "execution_mode",
        "label": "Execution mode",
        "section": "Execution & sizing",
        "kind": "enum",
        "unit": "",
        "options": ["OFF", "ANALYZE", "RECOMMEND", "PAPER_AUTO"],
        "path": ("autonomy", "mode"),
        "help": "Risk Manager mode. Only PAPER_AUTO may submit new paper orders.",
    },
    {
        "name": "reduce_fraction_pct",
        "label": "Partial reduction size",
        "section": "Execution & sizing",
        "kind": "number",
        "unit": "%",
        "minimum": 0.1,
        "maximum": 99.9,
        "step": 0.1,
        "path": ("paper_execution", "reduce_fraction_pct"),
        "help": "Position fraction closed by a REDUCE decision.",
    },
    {
        "name": "base_position_capital_pct",
        "label": "Base capital / position",
        "section": "Execution & sizing",
        "kind": "number",
        "unit": "%",
        "minimum": 0.05,
        "maximum": 20.0,
        "step": 0.05,
        "path": ("agents", "execution", "balance_usage_pct"),
        "help": "Target capital before confidence and performance reductions; it cannot exceed the Constitution position cap.",
    },
    {
        "name": "unprofitable_position_cap_pct",
        "label": "Unprofitable-strategy cap",
        "section": "Execution & sizing",
        "kind": "number",
        "unit": "%",
        "minimum": 0.05,
        "maximum": 20.0,
        "step": 0.05,
        "path": ("agents", "execution", "unprofitable_balance_usage_cap_pct"),
        "mirror_paths": (("agents", "execution", "unprofitable_balance_usage_hard_cap_pct"),),
        "help": "Maximum capital while the exact strategy/direction lacks profitable settled evidence.",
    },
    {
        "name": "force_one_x_until_profitable",
        "label": "Force 1x until profitable",
        "section": "Execution & sizing",
        "kind": "boolean",
        "unit": "",
        "path": ("agents", "execution", "force_one_x_until_profitable"),
        "help": "Keep leverage at 1x until the settled strategy/direction profile is profitable.",
    },
    {
        "name": "require_ai_review",
        "label": "Require AI review",
        "section": "Admission & cadence",
        "kind": "boolean",
        "unit": "",
        "path": ("agents", "execution", "autonomous_require_ai_review"),
        "help": "Require the independent AI review stage before Risk and Capital submission.",
    },
    {
        "name": "entry_cooldown_minutes",
        "label": "Entry cooldown",
        "section": "Admission & cadence",
        "kind": "number",
        "unit": "minutes",
        "minimum": 0.0,
        "maximum": 1440.0,
        "step": 1.0,
        "path": ("agents", "execution", "autonomous_cooldown_minutes"),
        "help": "Minimum delay between automated reviews/submissions for the same setup.",
    },
    {
        "name": "ticker_reentry_lockout_hours",
        "label": "Post-close ticker lockout",
        "section": "Admission & cadence",
        "kind": "number",
        "unit": "hours",
        "minimum": 0.0,
        "maximum": 168.0,
        "step": 0.5,
        "path": ("agents", "execution", "autonomous_ticker_lockout_hours"),
        "default": 6.0,
        "help": "Block automated re-entry in a ticker for this many hours after a close; 0 disables the lockout.",
    },
    {
        "name": "allow_scale_in",
        "label": "Allow scale-in",
        "section": "Admission & cadence",
        "kind": "boolean",
        "unit": "",
        "path": ("agents", "execution", "autonomous_allow_scale_in"),
        "help": "Allow a new automated order when that ticker already has an open position.",
    },
    {
        "name": "max_candidate_reviews_per_cycle",
        "label": "Candidate reviews / cycle",
        "section": "Admission & cadence",
        "kind": "integer",
        "unit": "reviews",
        "minimum": 0,
        "maximum": 1000,
        "step": 1,
        "path": ("agents", "execution", "max_autonomous_candidates_per_cycle"),
        "help": "Maximum ranked candidates reviewed per cycle; 0 means no review cap.",
    },
    {
        "name": "max_trade_submissions_per_cycle",
        "label": "Trade submissions / cycle",
        "section": "Admission & cadence",
        "kind": "integer",
        "unit": "orders",
        "minimum": 1,
        "maximum": 100,
        "step": 1,
        "path": ("agents", "execution", "max_autonomous_trades_per_cycle"),
        "help": "Hard maximum of new automated paper-order attempts in one cycle.",
    },
    {
        "name": "scanner_source_enabled",
        "label": "Scanner candidates",
        "section": "Candidate sources",
        "kind": "boolean",
        "unit": "",
        "path": ("agents", "execution", "autonomous_sources", "scanner"),
        "help": "Allow qualified Market Scanner candidates into the common Risk Manager pipeline.",
    },
    {
        "name": "watchlist_source_enabled",
        "label": "Watchlist candidates",
        "section": "Candidate sources",
        "kind": "boolean",
        "unit": "",
        "path": ("agents", "execution", "autonomous_sources", "watchlist"),
        "help": "Allow qualified Watchlist candidates into the common Risk Manager pipeline.",
    },
    {
        "name": "pre_earnings_source_enabled",
        "label": "Pre-Earnings candidates",
        "section": "Candidate sources",
        "kind": "boolean",
        "unit": "",
        "path": ("agents", "execution", "autonomous_sources", "pre_earnings"),
        "help": "Allow qualified Pre-Earnings candidates into the common Risk Manager pipeline.",
    },
    {
        "name": "trailing_stop_enabled",
        "label": "Automatic trailing stop",
        "section": "Exit management",
        "kind": "boolean",
        "unit": "",
        "path": ("bot", "trailing_enabled"),
        "help": "Permit the Exit Manager to tighten stops as positions move into profit.",
    },
    {
        "name": "scanner_time_stop_minutes",
        "label": "Scanner position time-stop",
        "section": "Exit management",
        "kind": "integer",
        "unit": "minutes",
        "minimum": 0,
        "maximum": 1440,
        "step": 15,
        "path": ("bot", "scanner_time_stop_minutes"),
        "help": "Close stale scanner positions after this duration; 0 disables time-based closure.",
    },
    {
        "name": "pre_earnings_post_event_ai_review",
        "label": "Post-earnings AI review",
        "section": "Exit management",
        "kind": "boolean",
        "unit": "",
        "path": ("bot", "pre_earnings_post_event_ai_review"),
        "help": "After results, let current news, price and volume evidence decide HOLD, REDUCE or CLOSE.",
    },
    {
        "name": "pre_earnings_exit_grace_minutes",
        "label": "Post-event review grace",
        "section": "Exit management",
        "kind": "integer",
        "unit": "minutes",
        "minimum": 0,
        "maximum": 1440,
        "step": 5,
        "path": ("bot", "pre_earnings_exit_grace_minutes"),
        "help": "Wait this long after the reported event before the evidence-based exit review starts.",
    },
    {
        "name": "pre_earnings_window_hours",
        "label": "Pre-close entry window",
        "section": "Pre-Earnings admission",
        "kind": "number",
        "unit": "hours",
        "minimum": 0.25,
        "maximum": 8.0,
        "step": 0.25,
        "path": ("agents", "execution", "pre_earnings_window_hours"),
        "default": 2.0,
        "help": "Length of the regular-session window before close in which a Pre-Earnings position may open.",
    },
    {
        "name": "pre_earnings_min_gap_risk",
        "label": "Minimum Gap-Risk",
        "section": "Pre-Earnings admission",
        "kind": "number",
        "unit": "score",
        "minimum": 0.0,
        "maximum": 100.0,
        "step": 0.5,
        "path": ("agents", "execution", "pre_earnings_min_gap_risk"),
        "help": "Candidate passes when Gap-Risk is greater than or equal to this value.",
    },
    {
        "name": "pre_earnings_min_abs_bias",
        "label": "Minimum absolute bias",
        "section": "Pre-Earnings admission",
        "kind": "number",
        "unit": "score",
        "minimum": 0.0,
        "maximum": 100.0,
        "step": 0.5,
        "path": ("agents", "execution", "pre_earnings_min_abs_bias"),
        "help": "LONG/SHORT directional bias must be greater than or equal to this threshold.",
    },
    {
        "name": "pre_earnings_min_coverage_score",
        "label": "Minimum evidence coverage",
        "section": "Pre-Earnings admission",
        "kind": "number",
        "unit": "score",
        "minimum": 0.0,
        "maximum": 100.0,
        "step": 0.5,
        "path": ("agents", "execution", "pre_earnings_min_coverage_score"),
        "help": "Minimum deterministic evidence coverage for entry consideration.",
    },
    {
        "name": "pre_earnings_min_score",
        "label": "Minimum execution score",
        "section": "Pre-Earnings admission",
        "kind": "number",
        "unit": "score",
        "minimum": 0.0,
        "maximum": 100.0,
        "step": 0.5,
        "path": ("agents", "execution", "pre_earnings_min_score"),
        "help": "Minimum canonical candidate execution score.",
    },
    {
        "name": "pre_earnings_require_canonical_decision",
        "label": "Require canonical AI OPEN",
        "section": "Pre-Earnings admission",
        "kind": "boolean",
        "unit": "",
        "path": ("agents", "execution", "pre_earnings_require_canonical_decision"),
        "help": "When enabled, WATCH is a hard veto. When disabled, AI remains advisory after deterministic gates pass.",
    },
    {
        "name": "pre_earnings_require_directional_edge",
        "label": "Require settled direction edge",
        "section": "Pre-Earnings admission",
        "kind": "boolean",
        "unit": "",
        "path": ("agents", "execution", "pre_earnings_require_directional_edge"),
        "help": "Require the selected LONG/SHORT direction to pass its settled-history thresholds.",
    },
    {
        "name": "pre_earnings_min_directional_sample",
        "label": "Minimum directional sample",
        "section": "Pre-Earnings admission",
        "kind": "integer",
        "unit": "events",
        "minimum": 1,
        "maximum": 100000,
        "step": 1,
        "path": ("agents", "execution", "pre_earnings_min_directional_sample"),
        "help": "Minimum settled LONG/SHORT observations when the direction-edge gate is enabled.",
    },
    {
        "name": "pre_earnings_min_directional_hit_rate",
        "label": "Minimum directional hit rate",
        "section": "Pre-Earnings admission",
        "kind": "number",
        "unit": "%",
        "minimum": 0.0,
        "maximum": 100.0,
        "step": 0.5,
        "path": ("agents", "execution", "pre_earnings_min_directional_hit_rate"),
        "help": "Minimum settled hit rate when the direction-edge gate is enabled.",
    },
)
RISK_TRADE_POLICY_FIELD_NAMES = tuple(
    field["name"] for field in RISK_TRADE_POLICY_SCHEMA
)
RISK_CONSTITUTION_PROFILES_FILENAME = "risk_constitution_profiles.json"
_RISK_PROFILE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class UnknownAgentError(ValueError):
    """Raised when a client tries to modify an unsupported worker."""


class RequiredAgentRunningError(RuntimeError):
    """Raised when a required safety worker is disabled during automation."""


class RiskConstitutionSettingsError(ValueError):
    """Raised when an operator draft cannot be safely persisted or activated."""


def _constitution_values(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    return {name: source.get(name) for name in RISK_CONSTITUTION_FIELD_NAMES}


def _normalize_constitution_candidate(values: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(values, Mapping):
        raise RiskConstitutionSettingsError("Risk Constitution values must be an object")
    missing = sorted(set(RISK_CONSTITUTION_FIELD_NAMES) - set(values))
    extra = sorted(set(values) - set(RISK_CONSTITUTION_FIELD_NAMES))
    if missing:
        raise RiskConstitutionSettingsError("Missing fields: " + ", ".join(missing))
    if extra:
        raise RiskConstitutionSettingsError("Unknown fields: " + ", ".join(extra))

    normalized: Dict[str, Any] = {}
    for field in RISK_CONSTITUTION_SCHEMA:
        name = str(field["name"])
        raw = values[name]
        if raw is None:
            normalized[name] = None
            continue
        kind = field["kind"]
        if kind == "boolean":
            if not isinstance(raw, bool):
                raise RiskConstitutionSettingsError(f"{field['label']} must be true or false")
            normalized[name] = raw
            continue
        if isinstance(raw, bool) or not isinstance(raw, Real):
            raise RiskConstitutionSettingsError(f"{field['label']} must be a JSON number")
        number = float(raw)
        if not math.isfinite(number):
            raise RiskConstitutionSettingsError(f"{field['label']} must be finite")
        if kind == "integer" and not number.is_integer():
            raise RiskConstitutionSettingsError(f"{field['label']} must be an integer")
        minimum = field.get("minimum")
        maximum = field.get("maximum")
        if minimum is not None and number < float(minimum):
            raise RiskConstitutionSettingsError(
                f"{field['label']} must be at least {minimum}"
            )
        if maximum is not None and number > float(maximum):
            raise RiskConstitutionSettingsError(
                f"{field['label']} must be at most {maximum}"
            )
        normalized[name] = int(number) if kind == "integer" else round(number, 6)

    per_trade = normalized["max_risk_per_trade_pct"]
    daily = normalized["max_daily_loss_pct"]
    if per_trade is not None and daily is not None and per_trade > daily:
        raise RiskConstitutionSettingsError(
            "Max risk / trade cannot exceed max daily loss"
        )
    position = normalized["max_position_size_pct"]
    exposure = normalized["max_total_exposure_pct"]
    if position is not None and exposure is not None and position > exposure:
        raise RiskConstitutionSettingsError(
            "Max position size cannot exceed max total exposure"
        )
    return normalized


_MISSING = object()


def _config_root(config: Any) -> Mapping[str, Any]:
    if isinstance(config, Mapping):
        return config
    root = getattr(config, "config", {}) or {}
    return root if isinstance(root, Mapping) else {}


def _value_at_path(root: Mapping[str, Any], path: tuple[str, ...], default: Any = _MISSING) -> Any:
    current: Any = root
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def _trade_policy_values_from_root(root: Mapping[str, Any]) -> Dict[str, Any]:
    """Resolve the legacy runtime mirrors into the logical Risk Manager policy."""

    try:
        from core.config import DEFAULT_CONFIG
    except Exception:  # pragma: no cover - core.config is part of every runtime
        DEFAULT_CONFIG = {}

    values: Dict[str, Any] = {}
    for field in RISK_TRADE_POLICY_SCHEMA:
        path = tuple(field["path"])
        raw = _value_at_path(root, path)
        if raw is _MISSING:
            raw = _value_at_path(DEFAULT_CONFIG, path)
        if raw is _MISSING:
            raw = field.get("default")
        values[str(field["name"])] = copy.deepcopy(raw)
    return values


def _normalize_trade_policy_candidate(values: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(values, Mapping):
        raise RiskConstitutionSettingsError("Risk Manager trade policy must be an object")
    missing = sorted(set(RISK_TRADE_POLICY_FIELD_NAMES) - set(values))
    extra = sorted(set(values) - set(RISK_TRADE_POLICY_FIELD_NAMES))
    if missing:
        raise RiskConstitutionSettingsError("Missing trade-policy fields: " + ", ".join(missing))
    if extra:
        raise RiskConstitutionSettingsError("Unknown trade-policy fields: " + ", ".join(extra))

    normalized: Dict[str, Any] = {}
    for field in RISK_TRADE_POLICY_SCHEMA:
        name = str(field["name"])
        raw = values[name]
        if raw is None:
            normalized[name] = None
            continue
        kind = str(field["kind"])
        if kind == "boolean":
            if not isinstance(raw, bool):
                raise RiskConstitutionSettingsError(f"{field['label']} must be true or false")
            normalized[name] = raw
            continue
        if kind == "enum":
            if not isinstance(raw, str):
                raise RiskConstitutionSettingsError(f"{field['label']} must be a string")
            option = raw.upper().strip()
            options = tuple(str(value) for value in field.get("options") or ())
            if option not in options:
                raise RiskConstitutionSettingsError(
                    f"{field['label']} must be one of: " + ", ".join(options)
                )
            normalized[name] = option
            continue
        if isinstance(raw, bool) or not isinstance(raw, Real):
            raise RiskConstitutionSettingsError(f"{field['label']} must be a JSON number")
        number = float(raw)
        if not math.isfinite(number):
            raise RiskConstitutionSettingsError(f"{field['label']} must be finite")
        if kind == "integer" and not number.is_integer():
            raise RiskConstitutionSettingsError(f"{field['label']} must be an integer")
        minimum = field.get("minimum")
        maximum = field.get("maximum")
        if minimum is not None and number < float(minimum):
            raise RiskConstitutionSettingsError(f"{field['label']} must be at least {minimum}")
        if maximum is not None and number > float(maximum):
            raise RiskConstitutionSettingsError(f"{field['label']} must be at most {maximum}")
        normalized[name] = int(number) if kind == "integer" else round(number, 6)

    base_size = normalized["base_position_capital_pct"]
    unprofitable_cap = normalized["unprofitable_position_cap_pct"]
    if base_size is not None and unprofitable_cap is not None and unprofitable_cap > base_size:
        raise RiskConstitutionSettingsError(
            "Unprofitable-strategy cap cannot exceed base capital / position"
        )
    return normalized


def _validate_trade_policy_against_constitution(
    policy: Mapping[str, Any],
    constitution: RiskConstitution,
) -> None:
    base_size = policy.get("base_position_capital_pct")
    if base_size is not None and float(base_size) > constitution.max_position_size_pct:
        raise RiskConstitutionSettingsError(
            "Base capital / position cannot exceed the Risk Constitution max position size"
        )


def _trade_policy_runtime_updates(policy: Mapping[str, Any]) -> list[tuple[list[str], Any]]:
    updates: list[tuple[list[str], Any]] = []
    for field in RISK_TRADE_POLICY_SCHEMA:
        value = policy[str(field["name"])]
        paths = (tuple(field["path"]), *tuple(field.get("mirror_paths") or ()))
        for path in paths:
            updates.append((list(path), copy.deepcopy(value)))
    return updates


def _public_trade_policy_schema() -> list[Dict[str, Any]]:
    hidden = {"path", "mirror_paths", "default"}
    return [
        {key: copy.deepcopy(value) for key, value in field.items() if key not in hidden}
        for field in RISK_TRADE_POLICY_SCHEMA
    ]


def _policy_fingerprint(values: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(values), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_CONSTITUTION_RUNTIME_PATHS = (
    ("agents", "risk", "max_daily_loss_pct"),
    ("agents", "risk", "max_daily_loss_trades"),
    ("agents", "risk", "max_risk_per_trade_pct"),
    ("agents", "risk", "max_position_size_pct"),
    ("agents", "risk", "max_exposure_pct"),
    ("agents", "risk", "max_open_trades"),
    ("agents", "risk", "max_drawdown_pct"),
    ("agents", "execution", "max_open_trades"),
    ("agents", "execution", "max_ai_leverage"),
    ("agents", "execution", "allow_ai_leverage"),
    ("bot", "circuit_breaker_daily_loss_pct"),
    ("bot", "circuit_breaker_drawdown_pct"),
    ("strategy_v2", "decision_max_leverage"),
    ("strategy_v2", "decision_allow_leverage"),
)


def risk_manager_owned_paths_in_patch(patch: Mapping[str, Any]) -> list[str]:
    """Return direct-config paths that must only be written through Risk Manager."""

    if not isinstance(patch, Mapping):
        return []
    protected = {
        ("risk_constitution",),
        ("risk_constitution_draft",),
        ("risk_trade_policy",),
        ("risk_trade_policy_draft",),
        *_CONSTITUTION_RUNTIME_PATHS,
    }
    for field in RISK_TRADE_POLICY_SCHEMA:
        protected.add(tuple(field["path"]))
        protected.update(tuple(path) for path in field.get("mirror_paths") or ())

    def _written_paths(value: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
        if not isinstance(value, Mapping):
            return [prefix]
        paths: list[tuple[str, ...]] = []
        for key, child in value.items():
            paths.extend(_written_paths(child, (*prefix, str(key))))
        return paths

    present: set[tuple[str, ...]] = set()
    for written in _written_paths(patch):
        for path in protected:
            # Block both a direct/descendant write and replacement of an
            # ancestor object (for example ``agents.execution = null``).
            common = min(len(written), len(path))
            if written[:common] == path[:common]:
                present.add(path if len(path) <= len(written) else written)
    return [".".join(path) for path in sorted(present)]


def assert_no_risk_manager_owned_updates(patch: Mapping[str, Any]) -> None:
    present = risk_manager_owned_paths_in_patch(patch)
    if present:
        preview = ", ".join(present[:8])
        suffix = " …" if len(present) > 8 else ""
        raise RiskConstitutionSettingsError(
            "These settings are owned by Risk Manager and cannot be changed through /api/config: "
            f"{preview}{suffix}. Use /api/agents/risk/constitution."
        )


def _risk_constitution_profiles_path() -> Path:
    configured = str(os.getenv("MS_RISK_CONSTITUTION_PROFILES_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[1] / RISK_CONSTITUTION_PROFILES_FILENAME


@lru_cache(maxsize=8)
def _load_risk_constitution_profiles_cached(
    path_string: str,
    modified_ns: int,
    size_bytes: int,
) -> tuple[Dict[str, Any], ...]:
    """Load and validate an immutable-by-convention profile snapshot.

    The stat values are part of the cache key, so operators can edit the JSON
    file without restarting the application. Profile selection remains a UI
    convenience only; this loader never writes owner settings.
    """
    del modified_ns, size_bytes
    path = Path(path_string)
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise RiskConstitutionSettingsError("Profile document must be a JSON object")
    if document.get("schema_version") != 1:
        raise RiskConstitutionSettingsError("Profile schema_version must be 1")
    raw_profiles = document.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise RiskConstitutionSettingsError("Profile document must contain profiles")

    profiles: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_profile in enumerate(raw_profiles, start=1):
        if not isinstance(raw_profile, Mapping):
            raise RiskConstitutionSettingsError(f"Profile {index} must be an object")
        profile_id = str(raw_profile.get("id") or "").strip()
        if not _RISK_PROFILE_ID.fullmatch(profile_id):
            raise RiskConstitutionSettingsError(
                f"Profile {index} id must use lowercase letters, numbers and hyphens"
            )
        if profile_id in seen:
            raise RiskConstitutionSettingsError(f"Duplicate profile id: {profile_id}")
        seen.add(profile_id)
        label = str(raw_profile.get("label") or "").strip()
        description = str(raw_profile.get("description") or "").strip()
        tone = str(raw_profile.get("tone") or "neutral").strip().lower()
        if not label or not description:
            raise RiskConstitutionSettingsError(
                f"Profile {profile_id} requires label and description"
            )
        if tone not in {"conservative", "balanced", "dynamic", "neutral"}:
            raise RiskConstitutionSettingsError(f"Profile {profile_id} has an unknown tone")
        normalized = _normalize_constitution_candidate(raw_profile.get("values"))
        if any(value is None for value in normalized.values()):
            raise RiskConstitutionSettingsError(
                f"Profile {profile_id} must provide every Risk Constitution value"
            )
        try:
            RiskConstitution.from_mapping({"owner_confirmed": True, **normalized})
        except RiskConstitutionError as exc:
            raise RiskConstitutionSettingsError(
                f"Profile {profile_id} is invalid: {exc}"
            ) from exc
        profiles.append(
            {
                "id": profile_id,
                "label": label,
                "description": description,
                "tone": tone,
                "values": normalized,
            }
        )
    return tuple(profiles)


def get_risk_constitution_profiles(path: Path | None = None) -> Dict[str, Any]:
    """Return hot-reloadable, validated Paper Trading templates."""
    source = path or _risk_constitution_profiles_path()
    try:
        stat = source.stat()
        profiles = _load_risk_constitution_profiles_cached(
            str(source.resolve()),
            stat.st_mtime_ns,
            stat.st_size,
        )
        return {
            "status": "ready",
            "source": source.name,
            "items": copy.deepcopy(list(profiles)),
            "error": None,
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.error("Risk Constitution profiles unavailable: %s", exc)
        return {
            "status": "error",
            "source": source.name,
            "items": [],
            "error": str(exc),
        }


def get_risk_constitution_settings(config: Any) -> Dict[str, Any]:
    """Return the single Risk Manager policy: Constitution plus trade controls."""
    root = _config_root(config)
    active_raw = root.get("risk_constitution")
    active_values = _constitution_values(active_raw)
    active = None
    active_error = None
    fingerprint = None
    if isinstance(active_raw, Mapping) and active_raw.get("owner_confirmed") is True:
        try:
            confirmed = RiskConstitution.from_mapping(active_raw)
            active = confirmed.to_dict()
            active_values = _constitution_values(active)
            fingerprint = confirmed.fingerprint
        except RiskConstitutionError as exc:
            active_error = str(exc)

    draft_present = isinstance(root.get("risk_constitution_draft"), Mapping)
    draft_raw = root.get("risk_constitution_draft") if draft_present else active_raw
    draft = _constitution_values(draft_raw)
    draft_error = None
    constitution_can_activate = False
    try:
        normalized = _normalize_constitution_candidate(draft)
        if all(value is not None for value in normalized.values()):
            RiskConstitution.from_mapping({"owner_confirmed": True, **normalized})
            constitution_can_activate = True
    except (RiskConstitutionSettingsError, RiskConstitutionError) as exc:
        draft_error = str(exc)

    runtime_policy = _trade_policy_values_from_root(root)
    policy_active_raw = root.get("risk_trade_policy")
    policy_active_source = "legacy_runtime"
    policy_active_error = None
    policy_active = None
    try:
        source = (
            {name: policy_active_raw.get(name) for name in RISK_TRADE_POLICY_FIELD_NAMES}
            if isinstance(policy_active_raw, Mapping)
            and policy_active_raw.get("owner_confirmed") is True
            else runtime_policy
        )
        policy_active = _normalize_trade_policy_candidate(source)
        if any(value is None for value in policy_active.values()):
            policy_active = None
        elif isinstance(policy_active_raw, Mapping) and policy_active_raw.get("owner_confirmed") is True:
            policy_active_source = "risk_manager"
    except RiskConstitutionSettingsError as exc:
        policy_active_error = str(exc)

    policy_draft_present = isinstance(root.get("risk_trade_policy_draft"), Mapping)
    policy_draft_raw = root.get("risk_trade_policy_draft") if policy_draft_present else (
        policy_active if policy_active is not None else runtime_policy
    )
    policy_draft = {
        name: policy_draft_raw.get(name) if isinstance(policy_draft_raw, Mapping) else None
        for name in RISK_TRADE_POLICY_FIELD_NAMES
    }
    policy_draft_error = None
    policy_can_activate = False
    try:
        normalized_policy_draft = _normalize_trade_policy_candidate(policy_draft)
        policy_can_activate = all(value is not None for value in normalized_policy_draft.values())
        if policy_can_activate and all(value is not None for value in draft.values()):
            candidate_constitution = RiskConstitution.from_mapping(
                {"owner_confirmed": True, **_normalize_constitution_candidate(draft)}
            )
            _validate_trade_policy_against_constitution(
                normalized_policy_draft, candidate_constitution
            )
    except (RiskConstitutionSettingsError, RiskConstitutionError) as exc:
        policy_draft_error = str(exc)
        policy_can_activate = False

    policy_draft_changed = (
        policy_draft_present
        and policy_active is not None
        and policy_draft != policy_active
    )
    if policy_active_error:
        policy_status = "invalid_active"
    elif policy_draft_changed:
        policy_status = "draft_changes"
    elif policy_active is not None:
        policy_status = "active" if policy_active_source == "risk_manager" else "legacy_runtime"
    elif any(value is not None for value in policy_draft.values()):
        policy_status = "draft"
    else:
        policy_status = "unconfigured"

    runtime_drift: list[str] = []
    if policy_active is not None and policy_active_source == "risk_manager":
        for field in RISK_TRADE_POLICY_SCHEMA:
            expected = policy_active[str(field["name"])]
            for path in (tuple(field["path"]), *tuple(field.get("mirror_paths") or ())):
                if _value_at_path(root, path) != expected:
                    runtime_drift.append(".".join(path))

    policy_fingerprint = _policy_fingerprint(policy_active) if policy_active is not None else None
    manager_fingerprint = None
    if active is not None and policy_active is not None:
        manager_fingerprint = _policy_fingerprint(
            {"constitution": active_values, "trade_policy": policy_active}
        )

    has_draft_values = any(value is not None for value in draft.values())
    draft_changed = bool(active) and draft_present and draft != active_values
    if active_error or policy_active_error or runtime_drift:
        status = "invalid_active"
    elif draft_changed or policy_draft_changed:
        status = "draft_changes"
    elif active:
        status = "active"
    elif has_draft_values:
        status = "draft"
    else:
        status = "unconfigured"

    return {
        "status": status,
        "active": active,
        "draft": draft,
        "schema": [dict(field) for field in RISK_CONSTITUTION_SCHEMA],
        "can_activate": constitution_can_activate and policy_can_activate,
        "fingerprint": fingerprint,
        "manager_fingerprint": manager_fingerprint,
        "active_error": active_error,
        "draft_error": draft_error,
        "profiles": get_risk_constitution_profiles(),
        "trade_policy": {
            "status": policy_status,
            "active": policy_active,
            "active_source": policy_active_source if policy_active is not None else None,
            "draft": policy_draft,
            "schema": _public_trade_policy_schema(),
            "can_activate": policy_can_activate,
            "fingerprint": policy_fingerprint,
            "active_error": policy_active_error,
            "draft_error": policy_draft_error,
            "runtime_drift": runtime_drift,
            "owner": "RISK_MANAGER",
        },
    }


def set_risk_constitution(
    *,
    config: Any,
    values: Mapping[str, Any],
    owner_confirmed: bool,
    trade_policy_values: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Atomically save/activate every Risk Manager-owned trading rule."""
    if not isinstance(owner_confirmed, bool):
        raise RiskConstitutionSettingsError("owner_confirmed must be boolean")
    candidate = _normalize_constitution_candidate(values)
    draft_document = {"owner_confirmed": False, **candidate}
    updates = [(["risk_constitution_draft"], draft_document)]
    policy_candidate = None
    if trade_policy_values is not None:
        policy_candidate = _normalize_trade_policy_candidate(trade_policy_values)
        updates.append((
            ["risk_trade_policy_draft"],
            {"owner_confirmed": False, **policy_candidate},
        ))
    synced_runtime_paths: list[str] = []

    if owner_confirmed:
        if any(value is None for value in candidate.values()):
            missing = [name for name, value in candidate.items() if value is None]
            raise RiskConstitutionSettingsError(
                "Complete every field before activation: " + ", ".join(missing)
            )
        try:
            constitution = RiskConstitution.from_mapping(
                {"owner_confirmed": True, **candidate}
            )
        except RiskConstitutionError as exc:
            raise RiskConstitutionSettingsError(str(exc)) from exc
        if policy_candidate is not None:
            if any(value is None for value in policy_candidate.values()):
                missing = [name for name, value in policy_candidate.items() if value is None]
                raise RiskConstitutionSettingsError(
                    "Complete every trade-policy field before activation: " + ", ".join(missing)
                )
            _validate_trade_policy_against_constitution(policy_candidate, constitution)
        active_document = constitution.to_dict()
        leverage = int(constitution.max_leverage)
        legacy_updates = [
            (["agents", "risk", "max_daily_loss_pct"], constitution.max_daily_loss_pct),
            (["agents", "risk", "max_daily_loss_trades"], constitution.max_daily_loss_trades),
            (["agents", "risk", "max_risk_per_trade_pct"], constitution.max_risk_per_trade_pct),
            (["agents", "risk", "max_position_size_pct"], constitution.max_position_size_pct),
            (["agents", "risk", "max_exposure_pct"], constitution.max_total_exposure_pct),
            (["agents", "risk", "max_open_trades"], constitution.max_positions),
            (["agents", "risk", "max_drawdown_pct"], constitution.max_drawdown_pct),
            (["agents", "execution", "max_open_trades"], constitution.max_positions),
            (["agents", "execution", "max_ai_leverage"], leverage),
            (["agents", "execution", "allow_ai_leverage"], leverage > 1),
            (["bot", "circuit_breaker_daily_loss_pct"], constitution.max_daily_loss_pct),
            (["bot", "circuit_breaker_drawdown_pct"], constitution.max_drawdown_pct),
            (["strategy_v2", "decision_max_leverage"], leverage),
            (["strategy_v2", "decision_allow_leverage"], leverage > 1),
        ]
        active_updates: list[tuple[list[str], Any]] = [
            (["risk_constitution"], active_document),
        ]
        policy_updates: list[tuple[list[str], Any]] = []
        if policy_candidate is not None:
            active_updates.append((
                ["risk_trade_policy"],
                {"owner_confirmed": True, **policy_candidate},
            ))
            policy_updates = _trade_policy_runtime_updates(policy_candidate)
        updates = [*active_updates, *updates, *legacy_updates, *policy_updates]
        synced_runtime_paths = [
            ".".join(path) for path, _value in [*legacy_updates, *policy_updates]
        ]

        if policy_candidate is not None and str(
            policy_candidate.get("execution_mode") or ""
        ).upper() == "PAPER_AUTO":
            # Parity with the removed /api/execution/control writer (fail-closed
            # autonomy): flipping PAPER_AUTO on is refused when entry readiness
            # is unmet (reconciliation UNVERIFIED, kill switch, execution agent
            # disabled or non-paper) instead of surfacing later as failed
            # orders. The candidate root carries the documents this call is
            # about to activate, so a first activation is judged against the
            # post-activation state, not the pre-activation one.
            from core.execution.policy import evaluate_paper_entry_readiness

            candidate_root = copy.deepcopy(getattr(config, "config", config))
            candidate_root["risk_constitution"] = active_document
            candidate_root["risk_trade_policy"] = {
                "owner_confirmed": True,
                **dict(policy_candidate),
            }
            # Mirror the runtime writes this activation performs so readiness
            # judges the post-activation runtime state (autonomy.mode, paper
            # sizing, execution agent), not the pre-activation one.
            for path, value in _trade_policy_runtime_updates(policy_candidate):
                target = candidate_root
                for part in path[:-1]:
                    target = target.setdefault(part, {})
                target[path[-1]] = copy.deepcopy(value)
            try:
                from core.execution.runtime import get_paper_broker

                reconciliation = get_paper_broker(config=config).store.get_reconciliation()
                reconciliation_status = str(reconciliation.get("status") or "UNVERIFIED")
            except Exception as exc:
                raise RiskConstitutionSettingsError(
                    f"PAPER_AUTO readiness could not be verified: {exc}"
                ) from exc
            readiness = evaluate_paper_entry_readiness(
                candidate_root, reconciliation_status=reconciliation_status
            )
            if not readiness.ready:
                raise RiskConstitutionSettingsError(
                    "PAPER_AUTO is blocked: " + "; ".join(readiness.reasons)
                )

    update_values = getattr(config, "update_values", None)
    if not callable(update_values):
        raise RuntimeError("Atomic config updates are unavailable")
    update_values(updates)
    return {
        "status": "updated",
        "mode": "active" if owner_confirmed else "draft",
        "applied_live": owner_confirmed,
        "constitution": get_risk_constitution_settings(config),
        "synced_legacy_guards": synced_runtime_paths,
        "synced_runtime_paths": synced_runtime_paths,
        "engine_apply_max_delay_seconds": 1.0 if owner_confirmed else None,
    }


def set_risk_settings(
    *,
    config: Any,
    max_daily_loss_pct: Any,
    max_risk_per_trade_pct: Any,
) -> Dict[str, Any]:
    """Compatibility adapter that updates the canonical Risk Constitution."""
    daily, per_trade = validate_risk_limits(
        max_daily_loss_pct,
        max_risk_per_trade_pct,
    )
    snapshot = get_risk_constitution_settings(config)
    active = snapshot.get("active")
    if not isinstance(active, Mapping):
        raise RiskConstitutionSettingsError(
            "Activate the complete Risk Manager policy before using the legacy two-field adapter"
        )
    values = _constitution_values(active)
    values["max_daily_loss_pct"] = daily
    values["max_risk_per_trade_pct"] = per_trade
    # Deliberately pass no trade policy: this two-field adapter must only
    # update the constitution, never rewrite the trade-policy document —
    # re-passing the active policy would stamp owner_confirmed=True onto a
    # policy the owner never confirmed field-by-field in Risk Manager.
    canonical = set_risk_constitution(
        config=config,
        values=values,
        owner_confirmed=True,
        trade_policy_values=None,
    )
    root = _config_root(config)
    risk_config = ((root.get("agents") or {}).get("risk") or {})
    bot_config = root.get("bot") or {}
    return {
        "status": "updated",
        "agent": "risk",
        "applied_live": True,
        "settings": {
            "max_daily_loss_pct": float(risk_config.get("max_daily_loss_pct", daily)),
            "max_risk_per_trade_pct": float(
                risk_config.get("max_risk_per_trade_pct", per_trade)
            ),
            "bot_circuit_breaker_daily_loss_pct": float(
                bot_config.get("circuit_breaker_daily_loss_pct", daily)
            ),
        },
        "independent_safeguards": {
            "max_daily_loss_trades": int(risk_config.get("max_daily_loss_trades", 2) or 2),
            "max_drawdown_pct": float(
                bot_config.get("circuit_breaker_drawdown_pct", 10.0) or 10.0
            ),
        },
        "constitution": canonical["constitution"],
        "synced_runtime_paths": canonical["synced_runtime_paths"],
        "engine_apply_max_delay_seconds": 1.0,
    }


_READINESS_REASON_PREFIXES = (
    "owner must select PAPER_AUTO",
    "STOP AUTONOMOUS TRADING is active",
    "Risk Constitution is invalid",
    "owner-confirmed Risk Constitution is required",
    "owner must select a position reduction percentage",
    "Execution Agent is disabled",
    "Execution Agent must be in paper mode",
    "EXECUTION_STATE_CONFLICT must be resolved",
    "paper broker reconciliation has not passed",
)


def _reconciliation_detail(reconciliation: Mapping[str, Any], status: str) -> str:
    conflicts = reconciliation.get("conflicts")
    conflicts = conflicts if isinstance(conflicts, list) else []
    if status == "EXECUTION_STATE_CONFLICT":
        parts = []
        for item in conflicts[:3]:
            if not isinstance(item, Mapping):
                continue
            code = str(item.get("code") or "conflict").replace("_", " ")
            refs = [
                str(item.get(key))
                for key in ("order_intent_id", "position_id")
                if item.get(key)
            ]
            parts.append(f"{code} ({', '.join(refs)})" if refs else code)
        extra = len(conflicts) - len(parts)
        listed = "; ".join(parts) + (f"; +{extra} more" if extra > 0 else "")
        return (
            f"{len(conflicts) or 1} order-ledger conflict(s) with Capital positions"
            + (f": {listed}" if listed else "")
        )
    if status == "HEALTHY":
        return "Order ledger matches Capital positions"
    return "Reconciliation has not passed yet"


def build_entry_gate(
    *,
    bot: Mapping[str, Any],
    engine_running: bool,
    agents_config: Mapping[str, Any],
    execution_control: Optional[Mapping[str, Any]],
    execution_control_error: Optional[str] = None,
) -> Dict[str, Any]:
    """One list of every gate a new paper entry must pass, each with its fix.

    The master card, the readiness panel and the Execution/Risk cards all read
    this object, so the tab can no longer say "entries enabled" on one card and
    "new paper risk blocked" on another. The readiness half is the same
    ``evaluate_paper_entry_readiness`` result the PaperBroker authorizer
    enforces before it accepts an order that opens new risk.
    """
    gates: list[Dict[str, Any]] = []

    def add(key: str, label: str, ready: bool, detail: str, action: str = "") -> None:
        gates.append(
            {
                "key": key,
                "label": label,
                "ready": bool(ready),
                "detail": detail,
                "action": "" if ready else action,
            }
        )

    execution_cfg = agents_config.get("execution") if isinstance(agents_config, Mapping) else {}
    execution_cfg = execution_cfg if isinstance(execution_cfg, Mapping) else {}
    risk_cfg = agents_config.get("risk") if isinstance(agents_config, Mapping) else {}
    risk_cfg = risk_cfg if isinstance(risk_cfg, Mapping) else {}
    mode = str(bot.get("mode") or "paper").lower()

    add(
        "market_engine",
        "Market engine",
        bool(engine_running),
        "Engine process is running" if engine_running else "The market engine process is not running",
        "Start the market engine (enable an engine-managed agent or restart the server).",
    )
    add(
        "master_switch",
        "Bot master switch",
        bool(bot.get("running")),
        "Bot is running" if bot.get("running") else "The master switch is off",
        "Press Start bot.",
    )
    add(
        "execution_agent",
        "Execution Agent",
        execution_cfg.get("enabled") is True,
        "Enabled" if execution_cfg.get("enabled") is True else "Execution Agent is disabled",
        "Turn on the Execution Agent switch.",
    )
    add(
        "risk_manager",
        "Risk Manager",
        risk_cfg.get("enabled") is True,
        "Enabled" if risk_cfg.get("enabled") is True else "Risk Manager is disabled",
        "Turn on the Risk Manager switch.",
    )
    worker_enabled = execution_cfg.get("autonomous_candidates_enabled") is True
    add(
        "candidate_worker",
        "Internal candidate worker",
        worker_enabled,
        "Enabled" if worker_enabled else "agents.execution.autonomous_candidates_enabled is off",
        "There is no switch for this in the app: set agents.execution.autonomous_candidates_enabled to true in config.json.",
    )
    breaker = bool(bot.get("circuit_breaker_active"))
    add(
        "circuit_breaker",
        "Circuit breaker",
        not breaker,
        "Not triggered" if not breaker else "Daily loss / drawdown breaker tripped today",
        "It re-arms on the next trading day; Start bot is refused until then.",
    )
    add(
        "paper_mode",
        "Paper mode",
        mode == "paper",
        "Paper" if mode == "paper" else f"Bot mode is {mode}",
        "Set the bot mode to paper.",
    )

    policy_verified = isinstance(execution_control, Mapping) and isinstance(
        execution_control.get("readiness"), Mapping
    )
    if not policy_verified:
        add(
            "risk_policy",
            "Risk Manager active policy",
            False,
            "Paper-entry readiness could not be verified"
            + (f": {execution_control_error}" if execution_control_error else ""),
            "Refresh this tab; if it persists check the server log.",
        )
    else:
        readiness = execution_control["readiness"]
        reasons = [str(item) for item in (readiness.get("reasons") or [])]
        autonomy_mode = str(readiness.get("mode") or "").upper()
        add(
            "execution_mode",
            "Execution mode",
            autonomy_mode == "PAPER_AUTO",
            f"Owner selected {autonomy_mode or 'no mode'}",
            "Open Risk Manager, set Execution mode to PAPER_AUTO, then Confirm and apply.",
        )
        stopped = bool(readiness.get("kill_switch_active"))
        add(
            "kill_switch",
            "New-entry stop",
            not stopped,
            "Released" if not stopped else "STOP AUTONOMOUS TRADING is active",
            "Open Risk Manager and press Resume new entries.",
        )
        invalid = next((r for r in reasons if r.startswith("Risk Constitution is invalid")), "")
        constitution_active = bool(readiness.get("risk_constitution_active")) and not invalid
        add(
            "risk_constitution",
            "Risk Manager active policy",
            constitution_active,
            "Owner-confirmed Risk Constitution is active"
            if constitution_active
            else (invalid or "No owner-confirmed Risk Constitution is active"),
            "Open Risk Manager, complete every limit and press Confirm and apply.",
        )
        reduce_ok = not any(
            r.startswith("owner must select a position reduction percentage") for r in reasons
        )
        add(
            "reduce_fraction",
            "Position reduction %",
            reduce_ok,
            f"{readiness.get('reduce_fraction_pct')}% per reduce order"
            if reduce_ok
            else "No valid position reduction percentage (0-100) is set",
            "Open Risk Manager, set the reduce fraction, then Confirm and apply.",
        )
        reconciliation_status = str(readiness.get("reconciliation_status") or "UNVERIFIED").upper()
        reconciliation = execution_control.get("reconciliation")
        reconciliation = reconciliation if isinstance(reconciliation, Mapping) else {}
        add(
            "broker_reconciliation",
            "Paper broker reconciliation",
            reconciliation_status == "HEALTHY",
            f"{reconciliation_status}: {_reconciliation_detail(reconciliation, reconciliation_status)}",
            "Open Risk Manager and press Reconcile broker. If the conflict is still listed, "
            "open the Capital tab and use \"Record orphaned-fill rollback\" on the reconciliation "
            "banner (an audit record only; cash and positions are not changed)."
            if reconciliation_status == "EXECUTION_STATE_CONFLICT"
            else "Open Risk Manager and press Reconcile broker.",
        )
        for reason in reasons:
            if reason.startswith(_READINESS_REASON_PREFIXES):
                continue
            add("policy", "Risk Manager policy", False, reason, "Open Risk Manager and resolve this item.")

    blockers = [gate for gate in gates if not gate["ready"]]
    is_open = not blockers
    if is_open:
        status = "open"
        summary = "New paper entries are enabled: every gate passed."
    else:
        status = "blocked" if policy_verified else "unverified"
        first = blockers[0]
        summary = f"New paper entries are blocked by {first['label']}: {first['detail']}."
    return {
        "open": is_open,
        "status": status,
        "policy_verified": policy_verified,
        "ready_count": len(gates) - len(blockers),
        "total": len(gates),
        "gates": gates,
        "blockers": blockers,
        "summary": summary,
    }


def build_control_plane_payload(
    *,
    config: Any,
    bot_status: Mapping[str, Any],
    agents_status: Mapping[str, Any],
    engine_running: bool,
    as_of: str | None = None,
    execution_control: Optional[Mapping[str, Any]] = None,
    execution_control_error: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a compact response with only settings rendered by the Agents UI.

    When the caller supplies the paper-execution control (or the error that
    prevented reading it) the payload carries ``entry_gate`` and the
    Execution/Risk ``accepting_new_entries`` flags follow it; without it the
    legacy bot/engine/breaker-only meaning is kept.
    """
    agents_config = getattr(config, "agents", {}) or {}
    root_config = getattr(config, "config", {}) or {}
    news_config = getattr(config, "news", {}) or {}
    strategy_config = getattr(config, "strategy_v2", {}) or {}
    bot_config = getattr(config, "bot", {}) or {}

    bot = dict(bot_status or {})
    bot["engine_running"] = bool(engine_running)
    autonomous_execution_active = bool(
        bot.get("running")
        and engine_running
        and not bot.get("circuit_breaker_active")
    )
    entry_gate: Optional[Dict[str, Any]] = None
    if execution_control is not None or execution_control_error is not None:
        entry_gate = build_entry_gate(
            bot=bot,
            engine_running=bool(engine_running),
            agents_config=agents_config if isinstance(agents_config, Mapping) else {},
            execution_control=execution_control,
            execution_control_error=execution_control_error,
        )
        # The PaperBroker refuses new risk while readiness is unmet, so the
        # bot being "on" is not enough to call autonomous execution active.
        autonomous_execution_active = bool(autonomous_execution_active and entry_gate["open"])
        bot["entries_enabled"] = bool(entry_gate["open"])
    bot["autonomous_execution_active"] = autonomous_execution_active
    agent_runtime = {
        key: dict(value or {})
        for key, value in (agents_status or {}).items()
    }
    # Every agent row carries the last-run outcome in one shape, so the Agents
    # tab can say "last run failed: ..." without probing for missing keys:
    # last_run_ok (True/False/None = no tracked run yet), last_run_error,
    # last_run_error_type, last_run_error_at, consecutive_failures,
    # last_run_finished_at.
    from api.agent_runtime_service import normalize_run_outcome

    for row in agent_runtime.values():
        row.update(normalize_run_outcome(row))
    for key in ("execution", "risk"):
        if key not in agent_runtime:
            continue
        worker_running = bool(agent_runtime[key].get("running"))
        agent_runtime[key]["accepting_new_entries"] = bool(
            worker_running and autonomous_execution_active
        )
        agent_runtime[key]["operating_mode"] = (
            "entry_enabled"
            if worker_running and autonomous_execution_active
            else ("monitoring_only" if worker_running else "stopped")
        )
        if entry_gate is not None and worker_running and not autonomous_execution_active:
            blockers = entry_gate.get("blockers") or []
            if blockers:
                agent_runtime[key]["entry_block_reason"] = str(blockers[0].get("label") or "")
    payload_extra: Dict[str, Any] = {}
    if entry_gate is not None:
        payload_extra["entry_gate"] = entry_gate
    return {
        "as_of": as_of or datetime.now(timezone.utc).isoformat(),
        "bot": bot,
        "agents": agent_runtime,
        **payload_extra,
        "config": {
            "agents": {
                key: dict(agents_config.get(key) or {})
                for key in CONTROL_PLANE_AGENTS
            },
            "bot": dict(bot_config),
            "news": {
                "news_refresh_minutes": news_config.get("news_refresh_minutes", 5),
            },
            "premarket": dict(root_config.get("premarket") or {}),
            "pre_earnings": dict(root_config.get("pre_earnings") or {}),
            "strategy_v2": {
                "research_agent": dict(strategy_config.get("research_agent") or {}),
            },
            "risk_constitution": get_risk_constitution_settings(config),
        },
    }


def set_agent_enabled(
    *,
    config: Any,
    agent_name: str,
    enabled: bool,
    bot_running: bool,
    status_updater: Callable[..., Any],
    runtime_switcher: Optional[Callable[[str, bool], Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Persist and immediately reconcile one supported agent toggle.

    Config writes are rolled back if the runtime transition fails, preventing a
    blue ON switch from claiming success while its worker is dead.
    """
    normalized = str(agent_name or "").strip().lower()
    if normalized not in CONTROL_PLANE_AGENTS:
        raise UnknownAgentError(normalized)
    if normalized in REQUIRED_WHILE_RUNNING and not enabled and bot_running:
        raise RequiredAgentRunningError(normalized)

    desired = bool(enabled)
    agents_config = getattr(config, "agents", {}) or {}
    current = agents_config.get(normalized, {}) if isinstance(agents_config, dict) else {}
    previous_enabled = bool(current.get("enabled", False)) if isinstance(current, dict) else bool(current)
    strategy_config = getattr(config, "strategy_v2", {}) or {}
    previous_research_enabled = bool(
        ((strategy_config.get("research_agent") or {}).get("enabled", previous_enabled))
        if isinstance(strategy_config, dict)
        else previous_enabled
    )

    config.update_value(["agents", normalized, "enabled"], desired)
    if normalized == "strategy_research":
        config.update_value(["strategy_v2", "research_agent", "enabled"], desired)

    runtime: Dict[str, Any] = {}
    try:
        if runtime_switcher is not None:
            runtime = dict(runtime_switcher(normalized, desired) or {})
    except Exception:
        config.update_value(["agents", normalized, "enabled"], previous_enabled)
        if normalized == "strategy_research":
            config.update_value(
                ["strategy_v2", "research_agent", "enabled"],
                previous_research_enabled,
            )
        status_updater(normalized, enabled=previous_enabled, running=False, lifecycle_state="error")
        raise

    status_fields = {key: value for key, value in runtime.items() if key != "enabled"}
    status_updater(normalized, enabled=desired, **status_fields)
    result = {"status": "updated", "agent": normalized, "enabled": desired}
    if runtime_switcher is not None:
        result["runtime"] = runtime
    return result
