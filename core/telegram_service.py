"""Telegram delivery service — routes app events to an owner-controlled chat.

Settings live in config.json under the "telegram" key (gitignored file):

    "telegram": {
        "enabled": false,
        "bot_token": "",            # or env TELEGRAM_BOT_TOKEN
        "chat_id": "",              # numeric chat / channel id
        "routes": {                 # what the owner wants delivered
            "news": true,           # breaking market news
            "signals": true,        # paper entries (OPEN_LONG/OPEN_SHORT)
            "capital": true,        # paper exits (CLOSE/REDUCE) + P&L
            "alerts": true          # Smart Alerts tab triggers (price/volatility)
        }
    }

notify_telegram() is fire-and-forget: it never blocks or raises in the
caller's path (engine loops and broker submissions call it). Sends are
serialized through one lock with a per-chat rate floor (Telegram allows
roughly 1 msg/s per chat).
"""

import hashlib
import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("TelegramService")

TELEGRAM_API_BASE = "https://api.telegram.org"
SEND_TIMEOUT_SECONDS = 8
MIN_SEND_INTERVAL_SECONDS = 1.1
MAX_MESSAGE_CHARS = 3900  # Telegram hard limit is 4096; keep headroom for markup

ROUTE_KEYS: Tuple[str, ...] = ("news", "signals", "capital", "alerts")

_DEFAULT_ROUTES: Dict[str, bool] = {key: True for key in ROUTE_KEYS}

_send_lock = threading.Lock()
_last_send_ts = 0.0

# BotFather tokens look like "<bot id>:<35 url-safe chars>". The check is
# deliberately loose on length but rejects the masked placeholder the UI shows
# ("8652…FgZU"), whitespace and anything that is obviously not a token.
_TOKEN_RE = re.compile(r"^\d{3,}:[A-Za-z0-9_-]{20,}$")
# Numeric user/group/channel id (channels are negative) or a public @username.
_CHAT_ID_RE = re.compile(r"^(-?\d{3,}|@[A-Za-z][A-Za-z0-9_]{3,})$")
_TOKEN_IN_TEXT_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_TAG_RE = re.compile(r"<[^>]+>")
_STATUS_FILE_NAME = "telegram_delivery_status.json"
_status_lock = threading.Lock()
MAX_LINE_CHARS = 600


def _redact(text: Any, token: str = "") -> str:
    """Remove the bot token from text that goes to logs or back to the UI.

    ``requests`` exceptions embed the full request URL — and the Telegram URL
    contains the token (``/bot<token>/sendMessage``)."""
    out = str(text or "")
    if token:
        out = out.replace(token, "***")
    return _TOKEN_IN_TEXT_RE.sub("bot***", out)


def _status_path() -> str:
    from core.config import DATA_DIR

    return str(DATA_DIR / _STATUS_FILE_NAME)


def _fingerprint(token: str, chat_id: str) -> str:
    """Stable id of one token+chat pair, so a delivery result recorded for an
    old bot/chat is never shown as proof that the current one works."""
    if not token or not chat_id:
        return ""
    return hashlib.sha256(f"{token}|{chat_id}".encode("utf-8")).hexdigest()[:16]


def _read_delivery_status() -> Dict[str, Any]:
    try:
        with open(_status_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _record_delivery(ok: bool, detail: str, token: str, chat_id: str) -> None:
    """Persist the outcome of the last real send attempt (shared by the API
    process and the engine subprocess, which both deliver messages)."""
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        fingerprint = _fingerprint(token, chat_id)
        with _status_lock:
            previous = _read_delivery_status()
            if previous.get("fingerprint") != fingerprint:
                previous = {}
            record = {
                "fingerprint": fingerprint,
                "ok": bool(ok),
                "detail": _redact(detail, token)[:300],
                "at": now_iso,
                "last_ok_at": now_iso if ok else previous.get("last_ok_at"),
                "last_error_at": previous.get("last_error_at") if ok else now_iso,
                "last_error": previous.get("last_error") if ok else _redact(detail, token)[:300],
            }
            path = _status_path()
            tmp_path = f"{path}.{os.getpid()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(record, handle)
            os.replace(tmp_path, path)
    except Exception:
        logger.debug("Could not persist Telegram delivery status", exc_info=True)


def _delivery_view(token: str, chat_id: str, enabled: bool) -> Tuple[str, Optional[Dict[str, Any]]]:
    """(delivery_state, last_delivery) for the settings payload.

    delivery_state is what the Alerts tab badge shows:
      not_configured — token or chat id missing
      off            — configured but delivery switched off
      unverified     — switched on, but no send has been attempted with this
                       token+chat, so nothing proves it works
      ok             — the last real send with this token+chat succeeded
      failing        — the last real send with this token+chat was rejected
    """
    if not token or not chat_id:
        return "not_configured", None
    status = _read_delivery_status()
    last: Optional[Dict[str, Any]] = None
    if status and status.get("fingerprint") == _fingerprint(token, chat_id):
        last = {
            "ok": bool(status.get("ok")),
            "detail": str(status.get("detail") or ""),
            "at": status.get("at"),
            "last_ok_at": status.get("last_ok_at"),
            "last_error_at": status.get("last_error_at"),
            "last_error": status.get("last_error"),
        }
    if not enabled:
        return "off", last
    if last is None:
        return "unverified", None
    return ("ok" if last["ok"] else "failing"), last


def _empty_settings() -> Dict[str, Any]:
    return {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "routes": dict(_DEFAULT_ROUTES),
    }


def get_telegram_settings(config: Any = None) -> Dict[str, Any]:
    """Read merged settings from config.json (never returns the raw token)."""
    raw: Dict[str, Any] = {}
    try:
        if config is None:
            from core.config import ConfigManager

            config = ConfigManager()
        raw = dict(getattr(config, "config", {}).get("telegram") or {})
    except Exception:
        logger.warning("Could not read telegram settings from config", exc_info=True)

    routes = dict(_DEFAULT_ROUTES)
    for key in ROUTE_KEYS:
        if key in (raw.get("routes") or {}):
            routes[key] = bool(raw["routes"][key])

    token = str(raw.get("bot_token") or "").strip() or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = str(raw.get("chat_id") or "").strip()
    enabled = bool(raw.get("enabled")) and bool(token) and bool(chat_id)
    delivery_state, last_delivery = _delivery_view(token, chat_id, enabled)
    return {
        "enabled": enabled,
        # What the owner asked for, even when it cannot take effect yet
        # (e.g. "enable" ticked but no chat id) — lets the UI explain why.
        "enabled_requested": bool(raw.get("enabled")),
        "configured": bool(token) and bool(chat_id),
        "has_token": bool(token),
        "delivery_state": delivery_state,
        "verified": delivery_state == "ok",
        "last_delivery": last_delivery,
        "bot_token_masked": (token[:4] + "…" + token[-4:]) if len(token) > 8 else ("set" if token else ""),
        "has_env_token": bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()),
        "chat_id": chat_id,
        "routes": routes,
    }


def save_telegram_settings(
    enabled: Optional[bool] = None,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    routes: Optional[Dict[str, bool]] = None,
    config: Any = None,
) -> Dict[str, Any]:
    """Persist settings atomically. An empty bot_token keeps the stored one.

    Raises ValueError for input that can never work (a masked placeholder or
    malformed token, a malformed chat id) so the stored, working values are
    not overwritten by it."""
    if bot_token is not None and str(bot_token).strip():
        if not _TOKEN_RE.match(str(bot_token).strip()):
            raise ValueError(
                "Bot token format is not valid (expected the BotFather token, "
                "e.g. 123456789:AA…). The stored token was kept."
            )
    if chat_id is not None and str(chat_id).strip():
        if not _CHAT_ID_RE.match(str(chat_id).strip()):
            raise ValueError(
                "Chat ID is not valid (expected a numeric id such as 123456789, "
                "-1001234567890 for a channel, or @channelname)."
            )
    if config is None:
        from core.config import ConfigManager

        config = ConfigManager()

    updates: List[Tuple[List[str], Any]] = []
    if enabled is not None:
        updates.append((["telegram", "enabled"], bool(enabled)))
    if bot_token is not None:
        token = str(bot_token).strip()
        if token:
            updates.append((["telegram", "bot_token"], token))
    if chat_id is not None:
        updates.append((["telegram", "chat_id"], str(chat_id).strip()))
    if routes is not None:
        clean: Dict[str, bool] = {}
        for key in ROUTE_KEYS:
            if key in routes:
                clean[key] = bool(routes[key])
        if clean:
            current = get_telegram_settings(config)["routes"]
            merged = {**current, **clean}
            updates.append((["telegram", "routes"], merged))

    if not updates:
        return get_telegram_settings(config)

    update_values = getattr(config, "update_values", None)
    if not callable(update_values):
        raise RuntimeError("Atomic config updates are unavailable")
    update_values(updates)
    return get_telegram_settings(config)


def _plain_text(text: str) -> str:
    """HTML-mode message → plain text (tags dropped, entities decoded)."""
    return html.unescape(_TAG_RE.sub("", str(text or "")))


def build_message(title: str, lines: Optional[List[str]] = None, footer: str = "") -> str:
    """Compose one HTML-parse-mode message from untrusted text.

    Titles and lines carry third-party text (news headlines, URLs, AI/broker
    reasons). Telegram rejects the whole message ("can't parse entities") when
    such text contains a stray ``<`` or ``&``, so every piece is escaped here
    and only this function adds markup. The body is shortened line by line so
    the limit never cuts through a tag or an entity."""
    safe_title = html.escape(str(title or "").strip()[:MAX_LINE_CHARS], quote=False)
    head = f"<b>{safe_title}</b>"
    tail = f"\n<i>{html.escape(str(footer), quote=False)}</i>" if footer else ""
    budget = MAX_MESSAGE_CHARS - len(head) - len(tail)
    body_parts: List[str] = []
    truncated = False
    for raw_line in lines or []:
        line = str(raw_line or "").strip()
        if not line:
            continue
        if len(line) > MAX_LINE_CHARS:
            line = line[: MAX_LINE_CHARS - 1] + "…"
        rendered = "\n• " + html.escape(line, quote=False)
        if len(rendered) > budget:
            truncated = True
            break
        body_parts.append(rendered)
        budget -= len(rendered)
    if truncated and budget >= 4:
        body_parts.append("\n• …")
    return head + "".join(body_parts) + tail


def _send_message(text: str, settings: Dict[str, Any]) -> Tuple[bool, str]:
    token = settings.get("bot_token") or ""
    if not token:
        # Re-read env lazily; get_telegram_settings masks the stored token so
        # callers pass the raw token via a fresh settings read only.
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = settings.get("chat_id") or ""
    if not token or not chat_id:
        return False, "Telegram is not configured (bot_token/chat_id missing)"

    text = str(text or "")
    parse_mode: Optional[str] = "HTML"
    if len(text) > MAX_MESSAGE_CHARS:
        # Cutting HTML mid-tag/entity makes Telegram reject the message, so an
        # over-long message is sent as plain text instead.
        text = _plain_text(text)[: MAX_MESSAGE_CHARS - 1] + "…"
        parse_mode = None

    def _post(body_text: str, mode: Optional[str]):
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": body_text,
            "disable_web_page_preview": True,
        }
        if mode:
            payload["parse_mode"] = mode
        return requests.post(
            f"{TELEGRAM_API_BASE}/bot{token}/sendMessage",
            json=payload,
            timeout=SEND_TIMEOUT_SECONDS,
        )

    def _describe(resp) -> str:
        detail = "http %s" % resp.status_code
        try:
            detail += ": %s" % (resp.json().get("description") or "")
        except Exception:
            pass
        return _redact(detail, token)

    global _last_send_ts
    with _send_lock:
        wait = MIN_SEND_INTERVAL_SECONDS - (time.time() - _last_send_ts)
        if wait > 0:
            time.sleep(wait)
        try:
            response = _post(text, parse_mode)
            _last_send_ts = time.time()
            if (
                response.status_code == 400
                and parse_mode
                and "parse entities" in _describe(response).lower()
            ):
                # Markup problem: deliver the content anyway, without markup.
                response = _post(_plain_text(text), None)
                _last_send_ts = time.time()
        except requests.RequestException as exc:
            # str(exc) contains the request URL, i.e. the bot token.
            detail = "network error: %s" % _redact(exc, token)
            logger.warning("Telegram send failed: %s", detail)
            _record_delivery(False, detail, token, chat_id)
            return False, detail

    if response.status_code == 200:
        _record_delivery(True, "sent", token, chat_id)
        return True, "sent"
    detail = _describe(response)
    logger.warning("Telegram send rejected (%s)", detail)
    _record_delivery(False, detail, token, chat_id)
    return False, detail


def test_telegram(settings: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """Send a test message with the current (or provided) settings."""
    if settings is None:
        settings = _raw_settings_for_send()
    ok, detail = _send_message(
        "✅ <b>Market Signals V9</b> — Telegram connected. The notifications"
        " selected in the Alerts tab will be delivered to this chat.",
        settings,
    )
    return ok, detail


def _raw_settings_for_send() -> Dict[str, Any]:
    """Read settings including the raw token (for the actual HTTP call)."""
    try:
        from core.config import ConfigManager

        raw = dict(getattr(ConfigManager(), "config", {}).get("telegram") or {})
    except Exception:
        raw = {}
    return {
        "bot_token": str(raw.get("bot_token") or "").strip(),
        "chat_id": str(raw.get("chat_id") or "").strip(),
    }


def notify_telegram(
    category: str,
    title: str,
    lines: Optional[List[str]] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort async delivery for an app event.

    Never raises, never blocks the caller: invalid category / disabled route /
    not configured are silent no-ops (debug-logged); the HTTP send runs on a
    short-lived daemon thread.
    """
    try:
        if category not in ROUTE_KEYS:
            logger.debug("Telegram notify ignored: unknown category %s", category)
            return
        resolved = settings or get_telegram_settings()
        if not resolved.get("enabled"):
            return
        if not resolved.get("routes", {}).get(category, False):
            return

        stamp = time.strftime("%H:%M:%S")
        text = build_message(title, lines, footer=f"Market Signals V9 · {stamp}")

        def _deliver() -> None:
            try:
                raw = settings
                if raw is None or "bot_token" not in raw:
                    raw = _raw_settings_for_send()
                ok, detail = _send_message(text, raw)
                if not ok:
                    logger.info("Telegram delivery not sent (%s/%s): %s", category, title, detail)
            except Exception:
                logger.warning("Telegram delivery crashed", exc_info=True)

        threading.Thread(target=_deliver, name="telegram-notify", daemon=True).start()
    except Exception:
        logger.warning("Telegram notify failed before dispatch", exc_info=True)
