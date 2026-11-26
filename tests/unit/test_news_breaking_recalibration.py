"""
Regression tests for the 2026-09-08 breaking-news recalibration.

Live evidence before the fix: 141 of 150 articles flagged is_breaking in the
news pool were eToro social posts (user opinions), pushed to Telegram as
"BREAKING". Root causes:
  1. Social posts were eligible for the BREAKING label.
  2. Keyword matching was raw substring over title+summary and included generic
     intensifiers ("hot", "important", "shock", "sudden") plus Fed terms.
Recalibration: social providers are never breaking; keywords are
word-boundary urgency markers; Fed/FOMC routes through the curated tier-1 list.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.news_tab_agent import NewsArticle, NewsTabAgent


def _agent() -> NewsTabAgent:
    return NewsTabAgent()


def _article(
    title: str,
    summary: str = "",
    provider: str = "yahoo_rss",
    age_minutes: int = 10,
    source: str = "Yahoo Finance",
) -> NewsArticle:
    return NewsArticle(
        id=f"test-{abs(hash(title)) % 10**10}",
        title=title,
        url="https://example.com/article",
        source=source,
        published_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        summary=summary,
        source_provider=provider,
    )


class TestSocialProviderGate:
    """eToro posts are sentiment intelligence, never wire breaking."""

    def test_etoro_post_with_urgency_words_is_not_breaking(self):
        agent = _agent()
        post = _article(
            title="BREAKING: NVDA will SURGE — this is the most important week",
            summary="Fed decision tomorrow, hot hot hot, everyone is shocked!",
            provider="etoro",
            source="eToro (@someuser)",
        )
        assert agent._is_breaking(post) is False

    def test_etoro_macro_tier1_mention_is_not_breaking(self):
        agent = _agent()
        post = _article(
            title="The most important week of Q3 starts today",
            summary="Wednesday brings CPI. Both drop before the Fed decision.",
            provider="etoro",
            source="eToro (@someuser)",
        )
        assert agent._is_breaking(post) is False


class TestKeywordBoundaryMatching:
    """Urgency markers must match on word boundaries; intensifiers are gone."""

    def test_genuine_breaking_wire_headline(self):
        agent = _agent()
        article = _article(title="BREAKING: Trading halted in regional bank shares")
        assert agent._is_breaking(article) is True

    def test_emergency_fed_action_is_breaking(self):
        agent = _agent()
        article = _article(
            title="Fed announces emergency rate cut",
            summary="The Federal Reserve cut rates by 50bp.",
        )
        assert agent._is_breaking(article) is True

    def test_generic_intensifiers_no_longer_trigger(self):
        agent = _agent()
        for title in (
            "The hot hotel stocks to watch this earnings season",
            "Why diversification is important for your portfolio",
            "Chip stocks sudden rise draws retail attention",
        ):
            assert agent._is_breaking(_article(title=title)) is False, title

    def test_move_descriptor_alone_is_not_breaking(self):
        agent = _agent()
        article = _article(
            title="Gold surges past $4,400 as dollar weakens",
            summary="The metal jumped for a third session.",
        )
        assert agent._is_breaking(article) is False


class TestMacroTier1StillBreaking:
    """Curated macro tier-1 events stay breaking for wire sources."""

    def test_cpi_report_is_breaking(self):
        agent = _agent()
        article = _article(
            title="CPI report shows inflation accelerating",
            summary="Consumer prices rose faster than expected in August.",
        )
        assert agent._is_breaking(article) is True

    def test_fomc_decision_is_breaking(self):
        agent = _agent()
        article = _article(
            title="FOMC cuts rates amid labor market slowdown",
            summary="Policymakers voted unanimously.",
        )
        assert agent._is_breaking(article) is True


class TestRecencyWindow:
    """The 72h eligibility cap still applies."""

    def test_old_urgent_headline_is_not_breaking(self):
        agent = _agent()
        article = _article(
            title="BREAKING: Trading halted in regional bank shares",
            age_minutes=73 * 60,
        )
        assert agent._is_breaking(article) is False
