import logging
import os
import threading
import time

from core.agent_toggles import is_agent_enabled

logger = logging.getLogger("BackgroundWorker")


class AnalysisWorker:
    """
    Lightweight background worker.
    Delegates work to NewsTabAgent async queues so the API thread stays responsive.
    """

    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self.news_agent = None
        self._last_error_text = ""
        self._last_error_logged_at = 0.0

    def _report_delegation_error(self, exc: Exception) -> None:
        """A failing analysis cycle must be visible in the log.

        It used to be logged at DEBUG every 10 seconds, i.e. never seen: the
        worker looked "started" while producing nothing. Warn once per distinct
        error and at most once every five minutes for a repeating one.
        """
        text = f"{type(exc).__name__}: {exc}"
        now = time.monotonic()
        if text != self._last_error_text or (now - self._last_error_logged_at) >= 300.0:
            logger.warning("Analysis worker cycle failed: %s", text)
            self._last_error_text = text
            self._last_error_logged_at = now

    def _get_news_agent_nonblocking(self):
        if not is_agent_enabled("news_tab", default=True):
            self.news_agent = None
            return None
        if self.news_agent is not None:
            return self.news_agent
        try:
            from agents.news_tab_agent import get_news_tab_agent_if_ready, kickoff_news_tab_agent_init
            self.news_agent = get_news_tab_agent_if_ready()
            if self.news_agent is None and os.getenv("MS_NEWS_AGENT_AUTOSTART", "0") == "1":
                kickoff_news_tab_agent_init()
        except Exception as e:
            logger.debug(f"News agent lazy-init check failed: {e}")
        return self.news_agent

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="NewsAnalysisWorker")
        self._thread.start()
        logger.info("Analysis Background Worker started.")

    def stop(self):
        if self._thread:
            self._stop_event.set()
            self._thread.join(timeout=2)
            logger.info("Analysis Background Worker stopped.")

    def _run(self):
        time.sleep(5)
        while not self._stop_event.is_set():
            try:
                agent = self._get_news_agent_nonblocking()
                if agent is not None:
                    agent._queue_auto_micro_summaries()
                    agent._queue_market_narrative_refresh()
            except Exception as e:
                self._report_delegation_error(e)
            for _ in range(10):
                if self._stop_event.is_set():
                    break
                time.sleep(1)


worker = AnalysisWorker()
