"""Build a local, searchable guide from TrendSpider Learning Center metadata.

The sync intentionally stores concise excerpts and document structure, not full
third-party article bodies. Full text remains available through the source URL.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


API_ROOT = "https://trendspider.com/learning-center/wp-json/wp/v2"
DEFAULT_OUTPUT = Path("data/knowledge/trendspider")
USER_AGENT = "MarketSignalsV7-KnowledgeSync/1.0 (+local research guide)"
MAX_EXCERPT_CHARS = 280
LOG = logging.getLogger("trendspider_guide")


def _plain_text(rendered_html: str) -> str:
    soup = BeautifulSoup(rendered_html or "", "html.parser")
    return re.sub(r"\s+", " ", html.unescape(soup.get_text(" ", strip=True))).strip()


def _headings(rendered_html: str) -> list[str]:
    soup = BeautifulSoup(rendered_html or "", "html.parser")
    values = [_plain_text(str(node)) for node in soup.find_all(["h2", "h3", "h4"])]
    return list(dict.fromkeys(value for value in values if value))


class TrendSpiderClient:
    def __init__(self, timeout: float = 30.0, retries: int = 3) -> None:
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def get(self, resource: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.session.get(
                    f"{API_ROOT}/{resource}", params=params, timeout=self.timeout
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError(f"Unexpected {resource} response shape")
                return payload
            except (requests.RequestException, ValueError) as exc:
                error = exc
                if attempt + 1 < self.retries:
                    time.sleep(2**attempt)
        raise RuntimeError(f"Could not fetch {resource}: {error}")

    def all_items(self, resource: str, fields: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.get(
                resource,
                {"per_page": 100, "page": page, "orderby": "id", "order": "asc", "_fields": fields},
            )
            if not batch:
                break
            items.extend(batch)
            LOG.info("Fetched %s %s records", len(items), resource)
            if len(batch) < 100:
                break
            page += 1
        return items


def build_records(
    posts: list[dict[str, Any]], categories: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    category_by_id = {int(item["id"]): item for item in categories}
    category_records = [
        {
            "id": int(item["id"]),
            "name": _plain_text(item.get("name", "")),
            "slug": item.get("slug", ""),
            "parent_id": int(item.get("parent", 0)),
            "article_count": int(item.get("count", 0)),
        }
        for item in categories
    ]
    records = []
    for post in posts:
        category_ids = [int(value) for value in post.get("categories", [])]
        excerpt = _plain_text(post.get("excerpt", {}).get("rendered", ""))
        records.append(
            {
                "id": int(post["id"]),
                "slug": post.get("slug", ""),
                "title": _plain_text(post.get("title", {}).get("rendered", "")),
                "source_url": post.get("link", ""),
                "published_at": post.get("date", ""),
                "updated_at": post.get("modified", ""),
                "category_ids": category_ids,
                "categories": [
                    _plain_text(category_by_id[value].get("name", ""))
                    for value in category_ids
                    if value in category_by_id
                ],
                "summary": excerpt[:MAX_EXCERPT_CHARS].rstrip(),
                "sections": _headings(post.get("content", {}).get("rendered", "")),
            }
        )
    return records, category_records


def write_guide(
    output_dir: Path,
    records: list[dict[str, Any]],
    categories: list[dict[str, Any]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "source": "TrendSpider Learning Center",
        "source_url": "https://trendspider.com/learning-center/",
        "generated_at": generated_at,
        "article_count": len(records),
        "category_count": len(categories),
        "format_version": 1,
        "content_policy": "Metadata, short excerpts, and headings only; follow source_url for full text.",
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "categories.json").write_text(
        json.dumps(categories, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (output_dir / "articles.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        names = record["categories"] or ["Uncategorized"]
        for name in names:
            grouped[name].append(record)
    lines = [
        "# TrendSpider Learning Center — ghid local",
        "",
        f"Actualizat: `{generated_at}`  ",
        f"Acoperire: **{len(records)} articole / {len(categories)} categorii**",
        "",
        "Acest catalog păstrează rezumate scurte și structura articolelor. Pentru explicația integrală,",
        "folosește linkul sursă asociat fiecărei intrări.",
        "",
    ]
    for category_name in sorted(grouped, key=str.casefold):
        lines.extend([f"## {category_name}", ""])
        for record in sorted(grouped[category_name], key=lambda item: item["title"].casefold()):
            lines.append(f"- [{record['title']}]({record['source_url']})")
        lines.append("")
    (output_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = TrendSpiderClient(timeout=args.timeout)
    categories = client.all_items("categories", "id,name,slug,parent,count")
    posts = client.all_items(
        "posts", "id,date,modified,slug,link,title,excerpt,categories,content"
    )
    records, category_records = build_records(posts, categories)
    if not records or not category_records:
        raise RuntimeError("Refusing to write an empty guide")
    write_guide(args.output.resolve(), records, category_records)
    LOG.info("Guide written to %s", args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
