# api/config_service.py - Configuration service for web API

import copy
import json
import logging
import math
from numbers import Real
from typing import Any, Dict, List, Tuple

from core.config import ConfigManager, CONFIG_PATH

logger = logging.getLogger("ConfigService")


def deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge two dictionaries.

    Args:
        base: Base dictionary
        override: Dictionary with values to override base

    Returns:
        Merged dictionary (override takes precedence)
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


_SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "api_token",
    "secret",
    "password",
    "passphrase",
    # Broker / messenger credentials that the old marker list let through in
    # full on GET /api/config (etoro.private_key, etoro.public_key,
    # telegram.bot_token).
    "private_key",
    "public_key",
    "bot_token",
    "access_token",
    "auth_token",
)
_REDACTED = "***REDACTED***"


class ConfigValidationError(ValueError):
    """Raised when an inbound config patch carries an unusable value."""


def _is_secret_key(key) -> bool:
    k = str(key).lower()
    # ``*_env`` entries (ai.providers.<name>.api_key_env, ollama.api_key_env)
    # hold the NAME of an environment variable, not a secret. Masking them made
    # every /api/ai/* route resolve provider keys through the literal variable
    # name "***REDACTED***": one pasted key was stored under that name and then
    # reported (and sent) as the key of every provider.
    if k.endswith("_env"):
        return False
    return any(marker in k for marker in _SECRET_KEY_MARKERS)


def redact_secrets(obj):
    """Deep-copy config with secret string values masked (safe for API responses)."""
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if _is_secret_key(k) and isinstance(v, str) and v else redact_secrets(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_secrets(v) for v in obj]
    return obj


def _strip_redacted(obj):
    """Drop redaction sentinels from inbound config so they never overwrite real secrets."""
    if isinstance(obj, dict):
        return {k: _strip_redacted(v) for k, v in obj.items() if v != _REDACTED}
    if isinstance(obj, list):
        return [_strip_redacted(v) for v in obj]
    return obj


def _reject_non_finite(obj: Any, path: Tuple[str, ...] = ()) -> None:
    """NaN/Infinity are accepted by Python's JSON parser but are not JSON.

    Persisting one makes config.json unreadable for every strict consumer
    (the browser's ``JSON.parse`` of GET /api/config included).
    """
    if isinstance(obj, float) and not math.isfinite(obj):
        raise ConfigValidationError(
            f"{'.'.join(path) or 'config'} must be a finite number"
        )
    if isinstance(obj, dict):
        for key, value in obj.items():
            _reject_non_finite(value, (*path, str(key)))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            _reject_non_finite(value, (*path, str(index)))


# Range contracts for the values the Global Settings form writes through
# POST /api/config. (label, minimum, minimum_inclusive, maximum, nullable)
_NUMERIC_RULES: Dict[Tuple[str, ...], Tuple[str, float, bool, float, bool]] = {
    ("signal_settings", "min_win_rate"): ("Min Win Rate", 0.0, True, 100.0, False),
    ("signal_settings", "profit_target_pct"): ("Take Profit", 0.0, False, 1000.0, True),
    ("signal_settings", "stop_loss_pct"): ("Stop Loss", 0.0, False, 100.0, True),
    ("agents", "risk", "position_size_pct"): ("Risk per Trade", 0.0, False, 100.0, False),
}


def _value_at(obj: Any, path: Tuple[str, ...]) -> Tuple[bool, Any]:
    current = obj
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def validate_config_patch(patch: Any) -> None:
    """Reject malformed inbound settings before anything is written."""
    if not isinstance(patch, dict):
        raise ConfigValidationError("config must be a JSON object")
    _reject_non_finite(patch)
    for path, (label, minimum, inclusive, maximum, nullable) in _NUMERIC_RULES.items():
        present, value = _value_at(patch, path)
        if not present:
            continue
        if value is None:
            if nullable:
                continue
            raise ConfigValidationError(f"{label} is required")
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ConfigValidationError(f"{label} must be a number")
        number = float(value)
        below = number < minimum if inclusive else number <= minimum
        if below or number > maximum:
            bound = f"{'>=' if inclusive else '>'} {minimum:g}"
            raise ConfigValidationError(f"{label} must be {bound} and <= {maximum:g}")


def _leaf_updates(
    patch: dict, current: Any, prefix: Tuple[str, ...] = ()
) -> List[Tuple[List[str], Any]]:
    """Flatten a deep-merge patch into per-leaf updates (same result as deep_merge)."""
    updates: List[Tuple[List[str], Any]] = []
    for key, value in patch.items():
        path = (*prefix, str(key))
        existing = current.get(key) if isinstance(current, dict) else None
        if isinstance(value, dict) and isinstance(existing, dict):
            # deep_merge recurses here; an empty override leaves the branch as is.
            updates.extend(_leaf_updates(value, existing, path))
        else:
            updates.append((list(path), value))
    return updates


def _json_equal(left: Any, right: Any) -> bool:
    try:
        return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
    except (TypeError, ValueError):
        return left == right


def get_config() -> dict:
    """
    Get current configuration using ConfigManager.

    Returns:
        Dictionary containing the current configuration, with secret
        values (API keys, tokens, passwords) masked.

    Raises:
        Exception: If configuration cannot be loaded
    """
    try:
        config_manager = ConfigManager(CONFIG_PATH)
        config_manager.reload_if_changed()

        return redact_secrets(config_manager._config)
    except Exception as e:
        logger.error(f"Error loading configuration: {e}")
        raise Exception(f"Failed to load configuration: {str(e)}")


def save_config(new_config: dict) -> bool:
    """
    Deep merge new configuration with existing configuration and save.

    The patch is validated first, then applied as one atomic multi-path update
    (re-reading the latest config under the cross-process write lock, so a
    concurrent Risk Manager / bot / engine write is not overwritten by a stale
    snapshot), and finally read back: ``ConfigManager`` logs but swallows disk
    errors, so without the read-back a failed write was reported as saved.

    Args:
        new_config: Dictionary with new/updated configuration values

    Returns:
        True if save was successful

    Raises:
        ConfigValidationError: the patch carries an unusable value
        Exception: If configuration cannot be saved
    """
    # Never let masked placeholders from GET /api/config overwrite real secrets
    new_config = _strip_redacted(new_config)
    validate_config_patch(new_config)

    try:
        config_manager = ConfigManager(CONFIG_PATH)
        config_manager.reload_if_changed()
        updates = _leaf_updates(new_config, config_manager._config)
        if not updates:
            return True

        config_manager.update_values(copy.deepcopy(updates))

        # Read the file itself (not another ConfigManager): the manager layers
        # env overrides and its SQLite snapshot over what is really on disk.
        with open(CONFIG_PATH, "r", encoding="utf-8") as config_file:
            persisted = json.load(config_file)
        missing = []
        for path, expected in updates:
            present, stored = _value_at(persisted, tuple(path))
            if not present or not _json_equal(stored, expected):
                missing.append(".".join(path))
        if missing:
            preview = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
            raise RuntimeError(f"configuration was not persisted ({preview})")

        logger.info("Configuration saved successfully")
        return True

    except Exception as e:
        logger.error(f"Error saving configuration: {e}")
        raise Exception(f"Failed to save configuration: {str(e)}")
