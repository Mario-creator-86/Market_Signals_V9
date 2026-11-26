"""
Test News Pruning Logic
Validates that old articles are automatically removed from news backup.
"""

import pytest
from datetime import datetime, timedelta, timezone
from agents.news_tab_agent import NewsTabAgent, NewsArticle, NewsCategory, NewsSentiment, NewsImpact


class TestNewsPruning:
    """Test news pruning functionality."""
    
    def test_prune_old_articles_removes_old_news(self):
        """Test that articles older than news_history_days are removed."""
        # Create agent with custom config (3 days retention for testing)
        config = {"news": {"news_history_days": 3}}
        agent = NewsTabAgent(config)
        
        # CRITICAL: Clear state loaded from backup to isolate test
        with agent._cache_lock:
            agent._state.ticker_news.clear()
            agent._state.market_news.clear()
            agent._state.breaking_news.clear()
        
        # Create test articles with different ages
        now = datetime.now(timezone.utc)
        
        # Recent article (2 days old) - should be KEPT
        recent_article = NewsArticle(
            id="recent-1",
            title="Recent News",
            url="https://example.com/recent",
            source="TestSource",
            published_at=now - timedelta(days=2),
            ticker="AAPL",
            category=NewsCategory.GENERAL,
            sentiment=NewsSentiment.NEUTRAL,
            impact=NewsImpact.LOW
        )
        
        # Old article (5 days old) - should be REMOVED
        old_article = NewsArticle(
            id="old-1",
            title="Old News",
            url="https://example.com/old",
            source="TestSource",
            published_at=now - timedelta(days=5),
            ticker="AAPL",
            category=NewsCategory.GENERAL,
            sentiment=NewsSentiment.NEUTRAL,
            impact=NewsImpact.LOW
        )
        
        # Manually populate state with both articles
        with agent._cache_lock:
            agent._state.ticker_news["AAPL"] = [recent_article, old_article]
            agent._state.market_news = [recent_article, old_article]
        
        # Assert both articles are present before pruning
        assert len(agent._state.ticker_news["AAPL"]) == 2
        assert len(agent._state.market_news) == 2
        
        # Execute pruning
        pruned_count = agent._prune_old_articles()
        
        # Assert old article was removed
        assert pruned_count == 2  # 1 from ticker_news + 1 from market_news
        assert len(agent._state.ticker_news["AAPL"]) == 1
        assert len(agent._state.market_news) == 1
        
        # Verify only recent article remains
        assert agent._state.ticker_news["AAPL"][0].id == "recent-1"
        assert agent._state.market_news[0].id == "recent-1"
    
    def test_prune_removes_empty_ticker_entries(self):
        """Test that ticker entries with no articles are removed."""
        config = {"news": {"news_history_days": 3}}
        agent = NewsTabAgent(config)
        
        # Clear state loaded from backup
        with agent._cache_lock:
            agent._state.ticker_news.clear()
            agent._state.market_news.clear()
            agent._state.breaking_news.clear()
        
        now = datetime.now(timezone.utc)
        
        # Old article that will be pruned
        old_article = NewsArticle(
            id="old-1",
            title="Old News",
            url="https://example.com/old",
            source="TestSource",
            published_at=now - timedelta(days=10),
            ticker="TSLA",
            category=NewsCategory.GENERAL,
            sentiment=NewsSentiment.NEUTRAL,
            impact=NewsImpact.LOW
        )
        
        with agent._cache_lock:
            agent._state.ticker_news["TSLA"] = [old_article]
        
        # Execute pruning
        agent._prune_old_articles()
        
        # Verify ticker entry was removed completely
        assert "TSLA" not in agent._state.ticker_news
    
    def test_prune_respects_config_parameter(self):
        """Test that pruning uses the news_history_days config parameter."""
        # Test with 1 day retention
        config = {"news": {"news_history_days": 1}}
        agent = NewsTabAgent(config)
        
        # Clear state loaded from backup
        with agent._cache_lock:
            agent._state.ticker_news.clear()
            agent._state.market_news.clear()
            agent._state.breaking_news.clear()
        
        now = datetime.now(timezone.utc)
        
        # Article from 2 days ago - should be removed with 1-day retention
        article = NewsArticle(
            id="test-1",
            title="Test",
            url="https://example.com/test",
            source="TestSource",
            published_at=now - timedelta(days=2),
            ticker="BTC-USD",
            category=NewsCategory.GENERAL,
            sentiment=NewsSentiment.NEUTRAL,
            impact=NewsImpact.LOW
        )
        
        with agent._cache_lock:
            agent._state.market_news = [article]
        
        # Execute pruning
        pruned_count = agent._prune_old_articles()
        
        # Verify article was removed
        assert pruned_count == 1
        assert len(agent._state.market_news) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
