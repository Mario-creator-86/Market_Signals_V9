# agents/base_agent.py

import logging
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from core.observability import update_agent_status

logger = logging.getLogger("MarketEngine")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BaseAgent(ABC):
    def __init__(self, config_manager, name="base"):
        self.config_manager = config_manager
        self.name = name
        self.running = False
        self.thread = None
        self._stop_event = threading.Event()

    @property
    def is_running(self):
        return self.running

    def start(self):
        if self.running:
            return
        self.running = True
        self._stop_event.clear()
        self.thread = threading.Thread(target=self._loop_wrapper, name=f"Agent-{self.name}")
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        self._stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        logger.info(f"Agent {self.name} stopped.")

    def _beat(self, **fields) -> None:
        """Per-cycle liveness heartbeat.

        The engine's agents live in the engine subprocess; the API derives
        their real running state from these heartbeats (via the
        agent_status_engine.json sidecar) instead of assuming every agent is
        alive while the engine process is up.
        """
        try:
            update_agent_status(self.name, running=True, last_heartbeat=_iso_now(), **fields)
        except Exception:
            pass  # diagnostics must never break the agent loop

    def _loop_wrapper(self):
        try:
            update_agent_status(self.name, running=True, initializing=False, last_heartbeat=_iso_now())
            self.loop()
        except Exception as e:
            logger.error(f"Agent {self.name} crashed: {e}")
            try:
                update_agent_status(
                    self.name, running=False, last_error=str(e)[:300], last_heartbeat=_iso_now()
                )
            except Exception:
                pass
            self.running = False
        finally:
            try:
                update_agent_status(self.name, running=False, last_heartbeat=_iso_now())
            except Exception:
                pass

    @abstractmethod
    def loop(self):
        """Main agent loop. Should check self._stop_event.is_set()."""
        pass
