# ui_news_adapter.py - adapter for displaying news in the UI

from __future__ import annotations

from typing import List, Tuple, Optional


def extract_news_lines(state: Optional[dict]) -> tuple[list[str], list[str]]:
    """
    Build from state['news']:
      - list of text lines for ScrolledText
      - list of URLs (in same order, one per line)
    If no news, returns ([], []).
    """
    lines: list[str] = []
    links: list[str] = []

    if not state or "news" not in state:
        return lines, links

    news = state.get("news", {}) or {}
    for ticker, articles in news.items():
        for art in articles or []:
            # Check if individual article is None before accessing
            if art is None:
                continue
            title = art.get("title")
            url = art.get("url")
            if not title or not url:
                continue
            pub = art.get("published_at", "")
            time_str = pub[:16].replace("T", " ") if isinstance(pub, str) and pub else ""
            line = f"{ticker} [{time_str}]: {title}"
            lines.append(line)
            links.append(url)

    return lines, links
