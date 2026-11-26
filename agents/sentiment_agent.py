# agents/sentiment_agent.py

import time
import logging
from typing import Dict

from .base_agent import BaseAgent

logger = logging.getLogger("MarketEngine")

# Only articles this recent count towards the market sentiment reading.
MARKET_SENTIMENT_MAX_AGE_MINUTES = 48 * 60


class SentimentAgent(BaseAgent):
    def __init__(self, config_manager):
        super().__init__(config_manager, "sentiment")
        self._last_sentiment: Dict[str, any] = {}

    def loop(self):
        """Fetch market sentiment from news agent and update shared state."""
        while not self._stop_event.is_set():
            try:
                self._beat()
                self._update_sentiment()
            except Exception as e:
                logger.error(f"SENTIMENT AGENT: error updating sentiment: {e}")
            cfg = self.config_manager.agents.get("sentiment", {})
            interval = int(cfg.get("refresh_seconds", 600) or 600)
            self._stop_event.wait(timeout=max(60, interval))

    def _update_sentiment(self):
        """Read sentiment data from news tab agent and compute market sentiment."""
        try:
            from agents.news_tab_agent import get_news_tab_agent_if_ready
            agent = get_news_tab_agent_if_ready()
            if agent is None:
                return

            state = agent.get_news_tab_state(active_tickers=None) or {}
            all_market_news = state.get("market_news", []) or []

            # "Market sentiment" must describe the current news flow. The feed
            # keeps 60 days of history and mixes in social posts; averaging the
            # newest 50 rows regardless of age reported weeks-old headlines and
            # users' opinions as today's market sentiment.
            market_news = []
            for article in all_market_news:
                try:
                    age_minutes = float(article.get("age_minutes"))
                except (TypeError, ValueError):
                    continue
                if age_minutes > MARKET_SENTIMENT_MAX_AGE_MINUTES:
                    continue
                if article.get("is_duplicate") or str(article.get("content_type") or "") == "social":
                    continue
                market_news.append(article)

            if not market_news:
                self._last_sentiment = {
                    "sentiment": "neutral",
                    "score": 0.0,
                    "articles": 0,
                    "window_hours": MARKET_SENTIMENT_MAX_AGE_MINUTES // 60,
                    "note": "no market-wide articles inside the window",
                    "timestamp": time.time(),
                }
                return

            # Compute aggregate sentiment from market news
            scores = []
            for article in market_news[:50]:
                sent = str(article.get("sentiment", "") or "").upper()
                if sent in ("VERY_BULLISH",):
                    scores.append(1.0)
                elif sent in ("BULLISH",):
                    scores.append(0.5)
                elif sent in ("NEUTRAL",):
                    scores.append(0.0)
                elif sent in ("BEARISH",):
                    scores.append(-0.5)
                elif sent in ("VERY_BEARISH",):
                    scores.append(-1.0)

            if not scores:
                self._last_sentiment = {
                    "sentiment": "neutral",
                    "score": 0.0,
                    "articles": len(market_news),
                }
                return

            avg_score = sum(scores) / len(scores)
            if avg_score > 0.3:
                label = "bullish"
            elif avg_score < -0.3:
                label = "bearish"
            else:
                label = "neutral"

            self._last_sentiment = {
                "sentiment": label,
                "score": round(avg_score, 3),
                "articles": len(market_news),
                "scored_articles": len(scores),
                "window_hours": MARKET_SENTIMENT_MAX_AGE_MINUTES // 60,
                "timestamp": time.time(),
            }
            logger.info(
                f"SENTIMENT AGENT: {label} ({avg_score:.3f}) from {len(scores)}/{len(market_news)} articles"
            )

        except Exception as e:
            logger.error(f"SENTIMENT AGENT: update failed: {e}")

    def get_sentiment(self) -> Dict[str, any]:
        """Get latest sentiment data."""
        if not self._last_sentiment:
            return {"sentiment": "neutral", "score": 0.0, "articles": 0, "stale": True}
        result = dict(self._last_sentiment)
        result["stale"] = False
        return result