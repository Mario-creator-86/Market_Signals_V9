"""
API Request Stats Logger Thread
Logs API request rates every 60 seconds for monitoring.
Stores 3-minute history for frontend display.
"""
import time
import threading
import logging
from collections import deque
from datetime import datetime

logger = logging.getLogger("MarketEngine")

# Global history storage (last 3 minutes of data points)
_api_stats_history = deque(maxlen=180)  # Store 180 seconds of data

def get_api_stats_history():
    """Return the last 3 minutes of API stats history"""
    return list(_api_stats_history)

def start_api_stats_logger(engine):
    """Start a background thread that logs API stats every 60 seconds"""
    def _log_stats():
        while True:
            time.sleep(60)
            try:
                # Yahoo RPM
                now_ts = time.time()
                cutoff = now_ts - 60
                yahoo_recent = [ts for ts in engine._yahoo_request_timestamps if ts > cutoff]
                yahoo_rpm = len(yahoo_recent)

                # Store in history
                data_point = {
                    "timestamp": datetime.now().isoformat(),
                    "yahoo_rpm": yahoo_rpm
                }
                _api_stats_history.append(data_point)

                logger.info(f"📊 API Stats: Yahoo {yahoo_rpm} req/min")
            except Exception as e:
                logger.debug(f"Stats logger error: {e}")

    thread = threading.Thread(target=_log_stats, daemon=True, name="APIStatsLogger")
    thread.start()
    logger.info("✅ API Stats Logger started (60s interval)")
