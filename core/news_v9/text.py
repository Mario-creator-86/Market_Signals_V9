"""Plain-text cleaning for ingested news (titles, excerpts, social posts).

Publisher excerpts arrived with markup ("<p style=...><img ...>") and
undecoded entities ("&#8230;", "&amp;", "&#39;") and were shown verbatim.
Cleaning happens once, at ingestion; the functions are idempotent on text
that is already clean.
"""
from __future__ import annotations

import html
import re
from typing import Any, List

# A tag starts with a letter, "/" or "!" - "price < 100 and > 50" is not markup.
_HTML_TAG_RE = re.compile(r"</?[A-Za-z!][^<>]{0,2000}>")
_ENTITY_HINT_RE = re.compile(r"&(?:#\d{1,7}|#x[0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
SOCIAL_TITLE_MAX_CHARS = 110


def clean_news_text(value: Any, keep_newlines: bool = False) -> str:
    """Source text as plain text: tags stripped, entities decoded, whitespace normalised.

    Tags are removed first and entities decoded once afterwards, so an escaped
    "&lt;b&gt;" in the text stays visible text instead of becoming markup.
    """
    text = str(value or "")
    if not text:
        return ""
    if "<" in text:
        text = _HTML_TAG_RE.sub(" ", text)
    if "&" in text and _ENTITY_HINT_RE.search(text):
        text = html.unescape(text)
    text = text.replace(" ", " ").replace("​", "")
    if keep_newlines:
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n")]
        out: List[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()
    return re.sub(r"\s+", " ", text).strip()


def truncate_at_word(text: Any, max_chars: int = SOCIAL_TITLE_MAX_CHARS) -> str:
    """Cut at a word boundary and mark the cut with an ellipsis."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= max_chars:
        return value
    cut = value[: max_chars - 1]
    space = cut.rfind(" ")
    if space >= max_chars // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-—–") + "…"


def social_post_title(title: Any, body: Any) -> str:
    """Headline of a social post: its first line, cut cleanly.

    The feed client builds titles as ``first line[:140]`` and, for a short
    first line, appends ``": " + second line[:80]`` - which produced
    "Sunday calm - weekend markets.: Flash: Nasdaq closed down 0.9% Friday as
    the 10-year yield topped 5% and Nvidia " (a ".: " join and a mid-word cut).
    The headline is rebuilt from the post's own first line, cut at a word
    boundary with an ellipsis, without the join.
    """
    lines = [ln.strip() for ln in clean_news_text(body, keep_newlines=True).split("\n") if ln.strip()]
    first = lines[0] if lines else ""
    raw_title = clean_news_text(title)
    if not first or len(first) < 3:
        first = raw_title
    elif raw_title and not (raw_title.startswith(first[:40]) or first.startswith(raw_title[:40])):
        # The title is not derived from this body: keep the title.
        first = raw_title
    return truncate_at_word(first, SOCIAL_TITLE_MAX_CHARS)
