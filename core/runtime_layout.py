"""Local runtime layout migration helpers.

Mutable operator state must not share source-controlled paths. Production uses
explicit volumes; local development uses ``runtime/`` and ``data/``. The
one-time copy is intentionally non-destructive so an interrupted upgrade can
always fall back to the legacy files.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import List, Tuple


LEGACY_RUNTIME_FILES = (
    "config.json",
    "watchlist.json",
    "state.json",
    "signals_state.json",
    "signals_state.bak.json",
    "signals_state.summary.json",
    "capital.json",
    "presets.json",
    "hotlist_cache.json",
    "suppressed_tickers.json",
)


def _copy_if_absent(source: Path, destination: Path) -> bool:
    if not source.is_file() or destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".migrate",
        dir=str(destination.parent),
    )
    os.close(fd)
    temp_path = Path(raw_temp)
    try:
        shutil.copy2(source, temp_path)
        # A concurrent API/engine bootstrap may have completed the same copy.
        # Both sources are identical legacy snapshots, so atomic replacement is
        # safe and leaves no partially-written destination.
        os.replace(temp_path, destination)
        return True
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def migrate_legacy_runtime_layout(
    base_dir: Path,
    runtime_dir: Path,
    data_dir: Path,
) -> List[Tuple[Path, Path]]:
    """Copy legacy root runtime files into dedicated mutable directories.

    Existing destinations always win, and legacy sources are retained. The
    return value records only copies performed by this invocation.
    """

    base = Path(base_dir).resolve()
    runtime = Path(runtime_dir).resolve()
    data = Path(data_dir).resolve()
    copied: List[Tuple[Path, Path]] = []

    if runtime != base:
        for name in LEGACY_RUNTIME_FILES:
            source = base / name
            destination = runtime / name
            if _copy_if_absent(source, destination):
                copied.append((source, destination))

    legacy_calendar = base / "calendar_events_cache.json"
    calendar_destination = data / "calendar_events_cache.json"
    if data != base and _copy_if_absent(legacy_calendar, calendar_destination):
        copied.append((legacy_calendar, calendar_destination))

    return copied
