"""
Quick verification script for News Impact Scoring V2.
Tests various real-world scenarios.
"""
from datetime import datetime, timezone
import sys
sys.path.insert(0, '.')

from agents.news_tab_agent import NewsArticle, NewsTabAgent, NewsImpact

agent = NewsTabAgent()

# Test various real-world scenarios
test_cases = [
    # (title, ticker, expected_range, expected_impact_description)
    ('Breaking: Fed raises rates by 50bps, markets tumble', None, (80, 100), 'CRITICAL'),
    ('CPI report shows inflation at 3.2%, below expectations', None, (80, 100), 'CRITICAL'),
    ('AAPL Q4 earnings beat expectations, stock rises 5%', 'AAPL', (65, 80), 'HIGH'),
    ('Analyst upgrades MSFT to outperform with $400 target', 'MSFT', (50, 60), 'HIGH'),
    ('Markets mixed ahead of Fed meeting', None, (15, 25), 'LOW'),
    ('FDA approves Pfizer cancer drug, shares surge', 'PFE', (95, 100), 'CRITICAL'),
    ('GDP growth 2.1% in Q3, economy shows resilience', None, (25, 40), 'MEDIUM'),
    ('NVDA announces AI partnership with Microsoft', 'NVDA', (40, 50), 'MEDIUM'),
    ('Merger: Company A to acquire Company B for $10B', 'ACQA', (65, 80), 'HIGH'),
]

print('=' * 90, flush=True)
print(f"{'Title':<50} | {'Ticker':<6} | {'Score':>5} | {'Impact':<8} | Status", flush=True)
print('=' * 90, flush=True)

all_passed = True
for title, ticker, expected_range, expected_impact in test_cases:
    article = NewsArticle(
        id='test',
        title=title,
        url='http://test.com',
        source='Test',
        published_at=datetime.now(timezone.utc),
        ticker=ticker
    )
    agent._analyze_article(article)
    
    in_range = expected_range[0] <= article.impact_score <= expected_range[1]
    correct_impact = article.impact.value == expected_impact.lower()
    status = 'PASS' if in_range and correct_impact else 'FAIL'
    if status == 'FAIL':
        all_passed = False
    
    print(f"{title[:50]:<50} | {str(ticker):<6} | {article.impact_score:>5} | {article.impact.value:<8} | {status}", flush=True)

print('=' * 90, flush=True)
print(f'\nAll tests passed: {all_passed}', flush=True)

