# core/execution_archive.py - Uncapped, append-only, asset-class-partitioned execution archive.

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from core.config import DATA_DIR
from core.strategy_v2.context_builder import infer_asset_class
from core.feedback_loop import FEEDBACK_JOURNAL_PATH
from core.trade_audit import TRADE_AUDIT_PATH

logger = logging.getLogger("ExecutionArchive")

ARCHIVE_DIR: Path = Path(DATA_DIR)  # configured data folder (MS_DATA_DIR)
STOCKS_FILE: Path = ARCHIVE_DIR / "executions_stocks.json"
CRYPTO_FILE: Path = ARCHIVE_DIR / "executions_crypto.json"
_BACKFILL_MARKER: Path = ARCHIVE_DIR / ".execution_archive_backfilled"

# Per-file locks guard the read-modify-write so concurrent appends never lose a record.
_FILE_LOCKS: dict[str, threading.Lock] = {}
_GLOBAL_LOCK = threading.Lock()  # guards _FILE_LOCKS dict creation
_JOURNAL_SUFFIX = ".append.jsonl"
_RECENT_JOURNAL_SUFFIX = ".recent.jsonl"
_RECENT_RECORDS_PER_ASSET = 500


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _route_file(asset_class: str) -> Path:
    return CRYPTO_FILE if str(asset_class or "").upper() == "CRYPTO_FUTURES" else STOCKS_FILE


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _GLOBAL_LOCK:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[key] = lock
        return lock


def _journal_path(path: Path) -> Path:
    """Return the durable append journal paired with a JSON snapshot."""
    return path.with_name(f"{path.name}{_JOURNAL_SUFFIX}")


def _recent_journal_path(path: Path) -> Path:
    """Return the bounded hot tail retained across explicit compaction."""
    return path.with_name(f"{path.name}{_RECENT_JOURNAL_SUFFIX}")


def _read_list(path: Path) -> list:
    """Load the archive list; re-initialize from [] on corruption (preserving the old file)."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        logger.warning(f"Execution archive {path.name} held non-list; re-initializing.")
        return []
    except Exception as exc:
        logger.warning(f"Execution archive {path.name} corrupt ({exc}); re-initializing and preserving old.")
        corrupt = path.with_name(f"{path.name}.corrupt-{int(datetime.now(timezone.utc).timestamp())}.json")
        try:
            path.replace(corrupt)
        except Exception:
            pass
        return []


def _write_list(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, indent=2, ensure_ascii=False)
        os.replace(tmp_name, str(path))
    except Exception as exc:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass
        raise


def _read_jsonl_file(journal: Path) -> list:
    """Read valid JSONL records, tolerating an interrupted final append.

    The main trade/capital state remains the system of record.  This archive is
    an additive audit projection, so an incomplete last line must not prevent
    older audit evidence from being read.
    """
    if not journal.exists():
        return []
    rows = []
    try:
        with journal.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    row = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("Ignoring malformed execution journal line %s in %s", line_number, journal.name)
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except Exception as exc:
        logger.warning("Could not read execution journal %s: %s", journal.name, exc)
    return rows


def _read_journal(path: Path) -> list:
    return _read_jsonl_file(_journal_path(path))


def _read_jsonl_tail(journal: Path, limit: int) -> list:
    """Read the newest JSONL rows without loading a growing audit archive.

    The append journal is the hot path.  Reading it from the end bounds the
    CPU and memory used by Capital's recent-decision view even when the full
    archive contains millions of historic events.
    """
    safe_limit = max(1, min(_RECENT_RECORDS_PER_ASSET, int(limit or 1)))
    if not journal.exists():
        return []
    rows: list = []
    try:
        with journal.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            pending = b""
            block_size = 65_536
            while position > 0 and len(rows) < safe_limit:
                read_size = min(block_size, position)
                position -= read_size
                handle.seek(position)
                pending = handle.read(read_size) + pending
                lines = pending.splitlines()
                # When this is not the first file block, the first element can
                # be a partial JSON line. Keep it until its prefix is read.
                if position > 0:
                    pending = lines.pop(0) if lines else pending
                else:
                    pending = b""
                for raw in reversed(lines):
                    if not raw.strip():
                        continue
                    try:
                        row = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
                        if len(rows) >= safe_limit:
                            break
            if position == 0 and pending.strip() and len(rows) < safe_limit:
                try:
                    row = json.loads(pending.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    row = None
                if isinstance(row, dict):
                    rows.append(row)
    except Exception as exc:
        logger.warning("Could not read recent execution journal %s: %s", journal.name, exc)
    return rows


def _write_jsonl_file(path: Path, rows: Iterable[dict]) -> None:
    """Atomically replace a bounded JSONL projection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                if isinstance(row, dict):
                    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
                    handle.write("\n")
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass
        raise


def _append_journal(path: Path, record: dict) -> None:
    """Append one audit record without rewriting the historical snapshot.

    Opening in append mode and closing immediately makes each record visible to
    the OS before the trading path continues, while avoiding the O(n²) read /
    serialize / replace cycle that made large archives unusable.
    """
    journal = _journal_path(path)
    journal.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with journal.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.write("\n")


def _merge_rows(snapshot: list, journal_rows: list) -> list:
    """Merge a snapshot and its journal without duplicate event IDs after compaction."""
    merged = list(snapshot)
    snapshot_ids = {
        str(row.get("event_id"))
        for row in snapshot
        if isinstance(row, dict) and row.get("event_id")
    }
    for row in journal_rows:
        event_id = str(row.get("event_id")) if row.get("event_id") else ""
        # Only suppress a journal row already materialized in the snapshot
        # during a compaction interruption.  Do not de-duplicate entries within
        # the journal: callers may legitimately record repeated event IDs from
        # distinct source streams, and the archive historically preserved them.
        if event_id and event_id in snapshot_ids:
            continue
        merged.append(row)
    return merged


def _read_rows(path: Path) -> list:
    return _merge_rows(_read_list(path), _read_journal(path))


def compact_execution_archive(asset_class: Optional[str] = None) -> int:
    """Materialize pending JSONL records into the backwards-compatible JSON snapshot.

    Compaction is explicit rather than per-event.  If the process stops between
    snapshot replacement and journal truncation, ``_merge_rows`` de-duplicates
    by event ID, so evidence is never lost or double-counted.
    """
    paths = []
    ac = str(asset_class).upper() if asset_class else None
    if ac is None or ac == "CRYPTO_FUTURES":
        paths.append(CRYPTO_FILE)
    if ac is None or ac != "CRYPTO_FUTURES":
        paths.append(STOCKS_FILE)

    compacted = 0
    for path in paths:
        lock = _lock_for(path)
        with lock:
            journal = _journal_path(path)
            journal_rows = _read_journal(path)
            if not journal_rows:
                continue
            rows = _merge_rows(_read_list(path), journal_rows)
            _write_list(path, rows)
            # Keep a compact, durable recent projection before truncating the
            # append journal.  It lets Capital show the latest evidence without
            # parsing the full historical JSON snapshot after compaction.
            hot_rows = _read_jsonl_file(_recent_journal_path(path))
            _write_jsonl_file(
                _recent_journal_path(path),
                (hot_rows + journal_rows)[-_RECENT_RECORDS_PER_ASSET:],
            )
            # Snapshot replacement succeeds before truncation.  A crash in this
            # gap is safe: the next read deduplicates the still-present journal.
            journal.write_text("", encoding="utf-8")
            compacted += len(journal_rows)
    return compacted


def append_execution(record: dict) -> None:
    """Append one record_trade_event record to the matching asset-class file.

    Stamps asset_class (resolved from record["ticker"] via infer_asset_class when absent),
    appends atomically, and never raises — trading must never break on an archive write.
    """
    try:
        if not isinstance(record, dict):
            return
        ticker = str(record.get("ticker") or "").strip()
        asset_class = record.get("asset_class")
        if not isinstance(asset_class, str) or not asset_class:
            asset_class = infer_asset_class(ticker) if ticker else "US_STOCKS"
        record = dict(record)
        record["asset_class"] = str(asset_class).upper()

        path = _route_file(record["asset_class"])
        lock = _lock_for(path)
        with lock:
            _append_journal(path, record)
    except Exception as exc:
        logger.error(f"Execution archive append failed: {exc}")


def read_executions(
    asset_class: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> list:
    """In-memory filter over one or both files by timestamp."""
    paths = []
    ac = str(asset_class).upper() if asset_class else None
    if ac is None or ac == "CRYPTO_FUTURES":
        paths.append(CRYPTO_FILE)
    if ac is None or ac != "CRYPTO_FUTURES":
        paths.append(STOCKS_FILE)

    out: list[dict] = []
    for path in paths:
        with _lock_for(path):
            out.extend(_read_rows(path))
    if since is None and until is None:
        return out

    def _in_window(ts_str: str) -> bool:
        try:
            dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        except Exception:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if since is not None and dt < since:
            return False
        if until is not None and dt > until:
            return False
        return True

    return [r for r in out if _in_window(r.get("timestamp", ""))]


def read_recent_executions(
    *,
    limit: int = 100,
    asset_class: Optional[str] = None,
    event_types: Optional[Iterable[str]] = None,
) -> list[dict]:
    """Return a bounded newest-first audit view without loading full history.

    This is intentionally separate from :func:`read_executions`, which remains
    the complete archive reader for offline/reporting tasks.  The API/UI path
    uses this bounded reader so it is safe as the append-only audit grows.
    """
    safe_limit = max(1, min(_RECENT_RECORDS_PER_ASSET, int(limit or 100)))
    requested_types = {str(item).strip().lower() for item in (event_types or []) if str(item).strip()}
    paths = []
    ac = str(asset_class).upper() if asset_class else None
    if ac is None or ac == "CRYPTO_FUTURES":
        paths.append(CRYPTO_FILE)
    if ac is None or ac != "CRYPTO_FUTURES":
        paths.append(STOCKS_FILE)

    # Request a little extra per source because filtering can remove records.
    per_path_limit = min(_RECENT_RECORDS_PER_ASSET, max(safe_limit * 3, safe_limit))
    rows: list[dict] = []
    seen: set[str] = set()
    for path in paths:
        with _lock_for(path):
            candidates = _read_jsonl_tail(_journal_path(path), per_path_limit)
            candidates.extend(_read_jsonl_tail(_recent_journal_path(path), per_path_limit))
        for row in candidates:
            event_id = str(row.get("event_id") or "")
            if event_id and event_id in seen:
                continue
            if requested_types and str(row.get("event_type") or "").strip().lower() not in requested_types:
                continue
            if event_id:
                seen.add(event_id)
            rows.append(row)
    rows.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
    return rows[:safe_limit]


def _parse_iso(value) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _legacy_event_id(source: str, row: dict, index: int) -> str:
    """Stable dedup key for backfilled rows (no event_id in legacy journal/audit)."""
    key = f"{source}:{row.get('ticker','')}:{row.get('closed_at','') or row.get('timestamp','')}:{index}"
    return "legacy_" + key


def backfill_from_legacy() -> int:
    """One-time idempotent import of trade_journal.json + trade_decisions.json.

    Dedups by event_id. Honors the marker file — returns 0 if already backfilled.
    Safe to re-run.
    """
    try:
        if _BACKFILL_MARKER.exists():
            return 0
        imported = 0
        seen: set[str] = set()

        # 1. Closed trades from the feedback journal (already carries asset_class).
        if Path(FEEDBACK_JOURNAL_PATH).exists():
            try:
                journal = json.loads(Path(FEEDBACK_JOURNAL_PATH).read_text(encoding="utf-8"))
            except Exception:
                journal = []
            if isinstance(journal, list):
                for i, row in enumerate(journal):
                    if not isinstance(row, dict):
                        continue
                    eid = _legacy_event_id("journal", row, i)
                    if eid in seen:
                        continue
                    seen.add(eid)
                    record = {
                        "event_id": eid,
                        "timestamp": str(row.get("closed_at") or _utc_now_iso()),
                        "event_type": "position_closed",
                        "status": "closed",
                        "ticker": str(row.get("ticker", "")).upper(),
                        "signal_id": "",
                        "position_id": "",
                        "strategy_type": str(row.get("strategy_type", "")).upper(),
                        "summary": str(row.get("close_reason", "")),
                        "payload": {"position": dict(row)},
                    }
                    append_execution(record)
                    imported += 1

        # 2. Capped audit log (trade_decisions.json) — preserves skips/opens too.
        if Path(TRADE_AUDIT_PATH).exists():
            try:
                payload = json.loads(TRADE_AUDIT_PATH.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            records = payload.get("records", []) if isinstance(payload, dict) else []
            if isinstance(records, list):
                for i, rec in enumerate(records):
                    if not isinstance(rec, dict):
                        continue
                    eid = str(rec.get("event_id") or _legacy_event_id("audit", rec, i))
                    if eid in seen:
                        continue
                    seen.add(eid)
                    append_execution(rec)
                    imported += 1

        compact_execution_archive()
        _BACKFILL_MARKER.write_text(_utc_now_iso(), encoding="utf-8")
        return imported
    except Exception as exc:
        logger.error(f"Execution archive backfill failed: {exc}")
        return 0
