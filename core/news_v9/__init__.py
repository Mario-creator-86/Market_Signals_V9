"""Phase 10 news normalization and financial-sentiment boundary."""

from core.news_v9.finbert import (
    FinancialSentimentResult,
    FinancialSentimentService,
    FinBERTAnalyzer,
    get_financial_sentiment_service,
)
from core.news_v9.pipeline import NewsPipeline, NewsPipelineResult

__all__ = [
    "FinancialSentimentResult",
    "FinancialSentimentService",
    "FinBERTAnalyzer",
    "NewsPipeline",
    "NewsPipelineResult",
    "get_financial_sentiment_service",
]
