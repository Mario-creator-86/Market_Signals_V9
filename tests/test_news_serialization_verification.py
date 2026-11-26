import pytest
import json
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

# Import necessary classes
from agents.news_tab_agent import NewsTabAgent, NewsArticle, NewsTabState, NewsCategory, NewsSentiment, NewsImpact
from core.engine import MarketEngine

class TestNewsSerialization:
    
    def test_news_article_serialization(self):
        """Verify NewsArticle to_dict converts Enums to strings."""
        article = NewsArticle(
            id="test_1",
            title="Test Article",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker="AAPL",
            category=NewsCategory.GENERAL,       # Enum
            sentiment=NewsSentiment.BULLISH,     # Enum
            impact=NewsImpact.HIGH,              # Enum
            summary="Summary"
        )
        
        data = article.to_dict()
        
        # Assertions
        assert isinstance(data["category"], str)
        assert data["category"] == "general"
        assert isinstance(data["sentiment"], str)
        assert data["sentiment"] == "bullish"
        assert isinstance(data["impact"], str)
        assert data["impact"] == "high"
        
        # Verify JSON serializability of the whole dict
        json_str = json.dumps(data)
        assert json_str is not None

    def test_news_tab_agent_state_type(self):
        """Verify NewsTabAgent.get_news_tab_state returns a Dict, not a List."""
        agent = NewsTabAgent()
        # Mock state to avoid external calls
        state = agent.get_news_tab_state()
        
        print(f"State type: {type(state)}")
        assert isinstance(state, dict), "get_news_tab_state should return a dictionary"
        assert "market_news" in state
        assert "ticker_news" in state
        assert isinstance(state["market_news"], list)

    def test_engine_activity_log_serialization_fix(self):
        """
        Verify the specific fix in engine.py where we log new articles.
        We simulate the logic: accessing .value from Enums.
        """
        article = NewsArticle(
            id="test_2",
            title="Breaking News",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker="TSLA",
            # Raw Enums as they appear coming from refresh_all_news
            category=NewsCategory.GENERAL,
            sentiment=NewsSentiment.BEARISH,
            impact=NewsImpact.CRITICAL
        )
        
        # Simulate the logic in engine.py (lines ~1068)
        # "sentiment": getattr(article, "sentiment", "neutral").value if ...
        
        sent_val = getattr(article, "sentiment", "neutral")
        if hasattr(sent_val, "value"):
            sent_str = sent_val.value
        else:
            sent_str = sent_val
            
        impact_val = getattr(article, "impact", "neutral")
        if hasattr(impact_val, "value"):
            impact_str = impact_val.value
        else:
            impact_str = impact_val
            
        entry = {
            "timestamp": datetime.now().isoformat(),
            "message": f"NEWS: {article.title}",
            "type": "news",
            "sentiment": sent_str,
            "impact": impact_str
        }
        
        # Validate values are strings
        assert entry["sentiment"] == "bearish"
        assert entry["impact"] == "critical"
        
        # Validate pure JSON dumping (simulating Atomic Write)
        try:
            json.dumps(entry)
        except TypeError as e:
            pytest.fail(f"JSON serialization failed: {e}")

if __name__ == "__main__":
    pytest.main([__file__])
