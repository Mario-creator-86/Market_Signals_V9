# Market Signals V7 - Agents Package
#
# Intentionally empty: eager re-exports here used to import six heavy agent
# modules (news, backtest, calendar, pre-earnings...) at package import time,
# defeating every lazy-import in the codebase. Import agents directly, e.g.
#   from agents.news_tab_agent import get_news_tab_agent
