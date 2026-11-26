"""Run the Strategy Lab once, in its own process.

    python -m core.strategy_lab.run [--no-refresh] [--market crypto|stocks] [--no-review]

The live app starts this as a child process once a day (core/strategy_lab/runner.py),
so that the search never competes with the trading loops for the interpreter.
It only reads public price data, the app's config and (read-only) the paper
positions; it writes the lab's own files under data/research/strategy_lab/.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional


def _config() -> Dict[str, Any]:
    try:
        from core.config import CONFIG_PATH

        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - defaults are fine
        return {}


def single_run_lock() -> Optional[int]:
    """An exclusive lock held for the whole run, or None when another lab run holds it.

    The run loads the registry at the start and saves it at the end, many minutes
    later: two runs at once would silently overwrite each other's decisions. The
    operating system releases the lock when the process ends, however it ends.
    """
    from core.strategy_lab.data import lab_dir

    descriptor = os.open(str(lab_dir() / "run.lock"), os.O_RDWR | os.O_CREAT)
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)            # always the same byte
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description="Strategy Lab: one run")
    parser.add_argument("--no-refresh", action="store_true", help="use the cached price history")
    parser.add_argument("--no-review", action="store_true", help="skip the research lead (finalists stay on watch)")
    parser.add_argument("--market", choices=["crypto", "stocks"], action="append")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    from core.strategy_lab.lab import run_once

    lock = single_run_lock()
    if lock is None:
        print("another Strategy Lab run is in progress; this one did not start")
        return 3
    ask = (lambda prompt, system, model: {"error": "review skipped (--no-review)"}) if args.no_review else None
    started = time.time()
    registry = run_once(config=_config(), refresh=False if args.no_refresh else None, markets=args.market, ask=ask)
    for record in registry["runs"][-len(args.market or ["crypto", "stocks"]):]:
        review = record.get("review") or {}
        print(f"{record['market']}: {record.get('rules_tested')} rules, {record.get('passed')} passed, "
              f"review {'ok' if review.get('available') else 'unavailable: ' + str(review.get('error'))}, "
              f"trading {record.get('trading')}, error '{record.get('error')}'")
    print(f"done in {time.time() - started:.0f}s")
    os.close(lock)
    if not registry.get("last_run_ok", True):
        print("the run is not complete (data refresh or a market failed); the scheduler will try again")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
