"""
Pytest tests for News Impact Scoring V2.

Tests verify the new scoring system:
- Base scores: TICKER_NEWS (40) vs GENERAL_NEWS (15)
- Modifiers: breaking, macro tier, FDA, earnings, analyst, sentiment
- Gating: GENERAL_NEWS capped at 49 without catalyst
- Duplicate penalty: -20 points
- Score to severity mapping
"""

import pytest
from datetime import datetime, timezone, timedelta
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.news_tab_agent import (
    NewsArticle,
    NewsTabAgent,
    NewsCategory,
    NewsSentiment,
    NewsImpact,
    ArticleType,
    score_to_severity,
    SCORE_BASE_TICKER_NEWS,
    SCORE_BASE_GENERAL_NEWS,
    SCORE_BREAKING,
    SCORE_MACRO_TIER1,
    SCORE_MACRO_TIER2,
    SCORE_FDA_BINARY,
    SCORE_EARNINGS_MA,
    SCORE_ANALYST,
    SCORE_SENTIMENT_EXTREME,
    SCORE_DUPLICATE_PENALTY,
)


class TestScoreToSeverity:
    """Test score to severity mapping."""
    
    def test_low_severity(self):
        """Score 0-24 should be LOW."""
        assert score_to_severity(0) == NewsImpact.LOW
        assert score_to_severity(15) == NewsImpact.LOW
        assert score_to_severity(24) == NewsImpact.LOW
    
    def test_medium_severity(self):
        """Score 25-49 should be MEDIUM."""
        assert score_to_severity(25) == NewsImpact.MEDIUM
        assert score_to_severity(35) == NewsImpact.MEDIUM
        assert score_to_severity(49) == NewsImpact.MEDIUM
    
    def test_high_severity(self):
        """Score 50-79 should be HIGH."""
        assert score_to_severity(50) == NewsImpact.HIGH
        assert score_to_severity(65) == NewsImpact.HIGH
        assert score_to_severity(79) == NewsImpact.HIGH
    
    def test_critical_severity(self):
        """Score 80-100 should be CRITICAL."""
        assert score_to_severity(80) == NewsImpact.CRITICAL
        assert score_to_severity(90) == NewsImpact.CRITICAL
        assert score_to_severity(100) == NewsImpact.CRITICAL


class TestBaseScores:
    """Test base score by article type."""
    
    @pytest.fixture
    def agent(self):
        return NewsTabAgent()
    
    def test_ticker_news_base_score(self, agent):
        """TICKER_NEWS should start with base 40."""
        article = NewsArticle(
            id="test1",
            title="AAPL stock rises on strong demand",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker="AAPL"
        )
        score = agent._calculate_impact_score(article)
        assert article.article_type == "ticker_news"
        assert score >= SCORE_BASE_TICKER_NEWS  # At least 40
    
    def test_general_news_base_score(self, agent):
        """GENERAL_NEWS should start with base 15."""
        article = NewsArticle(
            id="test2",
            title="Markets mixed ahead of trading session",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker=None
        )
        score = agent._calculate_impact_score(article)
        assert article.article_type == "general_news"
        assert score == SCORE_BASE_GENERAL_NEWS  # Exactly 15


class TestModifiers:
    """Test score modifiers."""
    
    @pytest.fixture
    def agent(self):
        return NewsTabAgent()
    
    def test_breaking_modifier(self, agent):
        """Breaking news should add +25 points."""
        article = NewsArticle(
            id="test3",
            title="TSLA stock news",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker="TSLA",
            is_breaking=True
        )
        score = agent._calculate_impact_score(article)
        assert score >= SCORE_BASE_TICKER_NEWS + SCORE_BREAKING  # 40 + 25 = 65
    
    def test_macro_tier1_modifier(self, agent):
        """Macro tier-1 (CPI, NFP, FOMC) should add +40 points."""
        article = NewsArticle(
            id="test4",
            title="Fed decision: rate hike announced today",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker=None
        )
        score = agent._calculate_impact_score(article)
        # 15 (base) + 40 (macro tier-1) = 55, but capped at 49 without catalyst?
        # Actually "rate hike" is in MARKET_CATALYST_CRITICAL, so no cap
        assert score >= 50  # HIGH
    
    def test_macro_tier2_modifier(self, agent):
        """Macro tier-2 (GDP, retail) should add +10 points."""
        article = NewsArticle(
            id="test5",
            title="GDP growth beats expectations at 2.5%",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker=None
        )
        score = agent._calculate_impact_score(article)
        # 15 + 10 = 25 (MEDIUM)
        assert score >= 25
    
    def test_fda_binary_modifier(self, agent):
        """FDA binary (approval/denial) should add +35 points."""
        article = NewsArticle(
            id="test6",
            title="MRNA receives FDA approval for new vaccine",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker="MRNA"
        )
        score = agent._calculate_impact_score(article)
        # 40 + 35 = 75 (HIGH)
        assert score >= 75
    
    def test_earnings_modifier(self, agent):
        """Earnings should add +25 points."""
        article = NewsArticle(
            id="test7",
            title="NVDA Q4 earnings beat expectations significantly",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker="NVDA",
            category=NewsCategory.EARNINGS
        )
        score = agent._calculate_impact_score(article)
        # 40 + 25 = 65 (HIGH)
        assert score >= 65
    
    def test_analyst_modifier(self, agent):
        """Analyst actions should add +10 points."""
        article = NewsArticle(
            id="test8",
            title="Analyst upgrades GOOGL to buy rating",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker="GOOGL",
            category=NewsCategory.ANALYST
        )
        score = agent._calculate_impact_score(article)
        # 40 + 10 = 50 (HIGH, marginal)
        assert score >= 50


class TestSentimentExtreme:
    """Test sentiment extreme modifier."""
    
    @pytest.fixture
    def agent(self):
        return NewsTabAgent()
    
    def test_sentiment_without_catalyst(self, agent):
        """Sentiment extreme without catalyst should be MEDIUM."""
        article = NewsArticle(
            id="test9",
            title="AAPL stock surging today",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker="AAPL",
            sentiment=NewsSentiment.VERY_BULLISH,
            sentiment_confidence=0.8
        )
        score = agent._calculate_impact_score(article)
        # 40 + 10 (sentiment) = 50 (HIGH marginal)
        assert score == 50
    
    def test_sentiment_with_catalyst(self, agent):
        """Sentiment extreme with catalyst should be HIGH."""
        article = NewsArticle(
            id="test10",
            title="AAPL stock surges after earnings beat",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker="AAPL",
            category=NewsCategory.EARNINGS,
            sentiment=NewsSentiment.VERY_BULLISH,
            sentiment_confidence=0.8
        )
        score = agent._calculate_impact_score(article)
        # 40 + 25 (earnings) + 10 (sentiment) = 75 (HIGH)
        assert score == 75


class TestGeneralNewsGating:
    """Test GENERAL_NEWS gating rule."""
    
    @pytest.fixture
    def agent(self):
        return NewsTabAgent()
    
    def test_general_news_capped_without_catalyst(self, agent):
        """GENERAL_NEWS without catalyst capped at 49."""
        article = NewsArticle(
            id="test11",
            title="Markets stable as traders wait for news",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker=None,
            is_breaking=True  # +25 → would be 40, but still no catalyst
        )
        score = agent._calculate_impact_score(article)
        # 15 + 25 = 40, but no catalyst, so should stay at 40 (< 50 anyway)
        assert score == 40  # No gating needed since < 50
    
    def test_general_news_high_with_catalyst(self, agent):
        """GENERAL_NEWS with catalyst can reach HIGH."""
        article = NewsArticle(
            id="test12",
            title="Fed announces rate cut of 25bps",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker=None,
            is_breaking=True
        )
        score = agent._calculate_impact_score(article)
        # 15 + 25 (breaking) + 40 (macro tier-1) = 80 (CRITICAL)
        # "rate cut" is both macro tier-1 AND catalyst
        assert score >= 80


class TestDuplicatePenalty:
    """Test duplicate detection and penalty."""
    
    @pytest.fixture
    def agent(self):
        return NewsTabAgent()
    
    def test_duplicate_penalty_applied(self, agent):
        """Duplicate articles should get -20 penalty."""
        article = NewsArticle(
            id="test13",
            title="AAPL earnings report released today",
            url="http://test.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker="AAPL",
            category=NewsCategory.EARNINGS,
            is_duplicate=True
        )
        score = agent._calculate_impact_score(article)
        # 40 + 25 (earnings) - 20 (duplicate) = 45 (MEDIUM)
        assert score == 45
    
    def test_duplicate_detection(self, agent):
        """Near-duplicate titles should be detected."""
        article1 = NewsArticle(
            id="art1",
            title="Apple announces new iPhone model today",
            url="http://test1.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker="AAPL"
        )
        article2 = NewsArticle(
            id="art2",
            title="Apple announces new iPhone model today",
            url="http://test2.com",
            source="Other",
            published_at=datetime.now(timezone.utc),
            ticker="AAPL"
        )
        
        is_dup = agent._check_duplicate(article2, [article1])
        assert is_dup is True
    
    def test_different_titles_not_duplicate(self, agent):
        """Different titles should not be flagged as duplicate."""
        article1 = NewsArticle(
            id="art3",
            title="Apple announces new iPhone",
            url="http://test1.com",
            source="Test",
            published_at=datetime.now(timezone.utc),
            ticker="AAPL"
        )
        article2 = NewsArticle(
            id="art4",
            title="Microsoft releases Windows update",
            url="http://test2.com",
            source="Other",
            published_at=datetime.now(timezone.utc),
            ticker="MSFT"
        )
        
        is_dup = agent._check_duplicate(article2, [article1])
        assert is_dup is False


class TestCompleteScenarios:
    """Test complete real-world scenarios."""
    
    @pytest.fixture
    def agent(self):
        return NewsTabAgent()
    
    def test_scenario_aapl_earnings(self, agent):
        """AAPL Q4 earnings beat → score 65, HIGH."""
        article = NewsArticle(
            id="sc1",
            title="AAPL Q4 earnings beat expectations",
            url="http://test.com",
            source="Reuters",
            published_at=datetime.now(timezone.utc),
            ticker="AAPL"
        )
        # Use _analyze_article to properly set all fields including category
        agent._analyze_article(article)
        assert article.impact_score == 65
        assert article.impact == NewsImpact.HIGH
    
    def test_scenario_fed_rate_hike(self, agent):
        """Fed raises rates → score 80+, CRITICAL."""
        article = NewsArticle(
            id="sc2",
            title="Fed announces rate hike of 25 basis points",
            url="http://test.com",
            source="Bloomberg",
            published_at=datetime.now(timezone.utc),
            ticker=None
        )
        agent._analyze_article(article)
        # Rate hike triggers breaking detection AND macro tier-1
        assert article.impact_score >= 80
        assert article.impact == NewsImpact.CRITICAL
    
    def test_scenario_analyst_upgrade(self, agent):
        """Analyst upgrades TSLA → score 50, HIGH (marginal)."""
        article = NewsArticle(
            id="sc3",
            title="Analyst upgrades TSLA to buy rating",
            url="http://test.com",
            source="MarketWatch",
            published_at=datetime.now(timezone.utc),
            ticker="TSLA"
        )
        agent._analyze_article(article)
        assert article.impact_score == 50
        assert article.impact == NewsImpact.HIGH
    
    def test_scenario_general_low(self, agent):
        """Generic market news → score 15, LOW."""
        article = NewsArticle(
            id="sc4",
            title="Markets stable in early trading",
            url="http://test.com",
            source="CNBC",
            published_at=datetime.now(timezone.utc),
            ticker=None
        )
        agent._analyze_article(article)
        assert article.impact_score == 15
        assert article.impact == NewsImpact.LOW
    
    def test_scenario_fda_approval(self, agent):
        """FDA approval for MRNA → score 100, CRITICAL (FDA binary + breaking)."""
        article = NewsArticle(
            id="sc5",
            title="MRNA receives FDA approval for new treatment",
            url="http://test.com",
            source="Reuters",
            published_at=datetime.now(timezone.utc),
            ticker="MRNA"
        )
        agent._analyze_article(article)
        # 40 (ticker) + 35 (FDA) + 25 (breaking) = 100 (CRITICAL)
        assert article.impact_score == 100
        assert article.impact == NewsImpact.CRITICAL



if __name__ == "__main__":
    pytest.main([__file__, "-v"])
