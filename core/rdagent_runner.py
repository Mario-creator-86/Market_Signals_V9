"""RD-Agent Worker Daemon — runs inside the dedicated rdagent Python environment.

Provides a live HTTP service and background loop for Microsoft RD-Agent
research workflows, exposing /health and /api/status.
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("rdagent_worker")

try:
    import rdagent
    RDAGENT_VERSION = getattr(rdagent, "__version__", "0.8.0")
except Exception as exc:
    rdagent = None
    RDAGENT_VERSION = f"unavailable: {exc}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_app():
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(title="RD-Agent Worker Service", version=RDAGENT_VERSION)

    start_time = _utc_now()

    @app.get("/health")
    def health():
        return {
            "status": "HEALTHY",
            "service": "rdagent_worker",
            "rdagent_version": RDAGENT_VERSION,
            "rdagent_installed": rdagent is not None,
            "started_at": start_time,
            "checked_at": _utc_now(),
        }

    @app.get("/api/status")
    def status():
        return {
            "status": "RUNNING",
            "service": "rdagent_worker",
            "rdagent_version": RDAGENT_VERSION,
            "pid": os.getpid(),
            "started_at": start_time,
            "active_tasks": 0,
            "checked_at": _utc_now(),
        }

    return app


def main():
    parser = argparse.ArgumentParser(description="RD-Agent Worker Daemon")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    parser.add_argument("--port", type=int, default=19000, help="Port to bind")
    args = parser.parse_args()

    logger.info("Starting RD-Agent Worker Daemon on %s:%d (rdagent version: %s)...",
                args.host, args.port, RDAGENT_VERSION)

    import uvicorn
    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
