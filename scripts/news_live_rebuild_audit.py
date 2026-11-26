import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.news_tab_agent import get_news_tab_agent


def load_watchlist() -> list[str]:
    p = ROOT / "watchlist.json"
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    stocks = data.get("yahoo", data.get("stocks", [])) or []
    crypto = data.get("binance", data.get("crypto", [])) or []
    order = data.get("order", []) or []
    merged: list[str] = []
    seen = set()
    for tk in list(order) + list(stocks) + list(crypto):
        t = str(tk or "").upper().strip()
        if not t or t in seen:
            continue
        seen.add(t)
        merged.append(t)
    return merged


def run_audit() -> dict:
    out_dir = ROOT / "output" / "news_audit"
    out_dir.mkdir(parents=True, exist_ok=True)

    watchlist = load_watchlist()
    agent = get_news_tab_agent()

    start = datetime.utcnow()
    agent.clear_cache()
    _, newly = agent.refresh_all_news(watchlist)
    end = datetime.utcnow()

    all_articles = agent._iter_all_articles()
    agent._organize_state(all_articles, watchlist=watchlist)
    agent._save_backup()

    bucket_counts = {tk: len(arr or []) for tk, arr in (agent._state.ticker_news or {}).items()}
    selected = sorted(bucket_counts, key=lambda k: bucket_counts[k], reverse=True)[:30]
    if "JMIA" in watchlist and "JMIA" not in selected:
        selected = (selected[:29] + ["JMIA"]) if len(selected) >= 30 else (selected + ["JMIA"])
    selected = list(dict.fromkeys(selected))

    id_to_buckets = defaultdict(set)
    for tk, arr in (agent._state.ticker_news or {}).items():
        for a in (arr or []):
            id_to_buckets[a.id].add(tk)

    rows = []
    for tk in selected:
        arr = list((agent._state.ticker_news or {}).get(tk, []) or [])
        suspect = [a for a in arr if not agent._is_ticker_mentioned_in_article(tk, a)]
        multi = sum(1 for a in arr if len(set(a.related_tickers or [])) >= 2)
        rows.append(
            {
                "ticker": tk,
                "count": len(arr),
                "suspect_count": len(suspect),
                "suspect_ratio": (len(suspect) / len(arr)) if arr else 0.0,
                "multi_count": multi,
                "sample_suspects": [
                    {
                        "id": s.id,
                        "title": s.title,
                        "related_tickers": list(s.related_tickers or []),
                        "source": s.source,
                        "published_at": s.published_at.isoformat() if s.published_at else None,
                    }
                    for s in suspect[:5]
                ],
            }
        )

    bucket_mismatch = []
    for tk, arr in (agent._state.ticker_news or {}).items():
        for a in (arr or []):
            if tk not in set(a.related_tickers or []):
                bucket_mismatch.append({"ticker": tk, "id": a.id, "title": a.title})

    market_with_related = [
        {"id": a.id, "title": a.title, "related_tickers": list(a.related_tickers or [])}
        for a in (agent._state.market_news or [])
        if (a.related_tickers or [])
    ]

    market_mentions_watchlist = []
    for a in (agent._state.market_news or []):
        mentions = [tk for tk in watchlist if agent._is_ticker_mentioned_in_article(tk, a)]
        if mentions:
            market_mentions_watchlist.append({"id": a.id, "title": a.title, "mentions": mentions[:8]})

    multi_expected_coverage_fail = []
    for tk, arr in (agent._state.ticker_news or {}).items():
        for a in (arr or []):
            rel = set(a.related_tickers or [])
            if len(rel) >= 2 and not rel.issubset(id_to_buckets.get(a.id, set())):
                multi_expected_coverage_fail.append(
                    {
                        "id": a.id,
                        "title": a.title,
                        "related": sorted(rel),
                        "actual_buckets": sorted(id_to_buckets.get(a.id, set())),
                    }
                )

    jmia = next(
        (x for x in rows if x["ticker"] == "JMIA"),
        {"count": 0, "suspect_count": 0, "suspect_ratio": 0.0, "sample_suspects": []},
    )

    summary = {
        "mode": "live_rebuild",
        "timestamp_utc": datetime.utcnow().isoformat() + "Z",
        "started_utc": start.isoformat() + "Z",
        "ended_utc": end.isoformat() + "Z",
        "elapsed_seconds": int((end - start).total_seconds()),
        "watchlist_count": len(watchlist),
        "new_articles_count": len(newly or []),
        "ticker_bucket_count": len((agent._state.ticker_news or {}).keys()),
        "ticker_article_total": sum(len(v or []) for v in (agent._state.ticker_news or {}).values()),
        "market_article_count": len(agent._state.market_news or []),
        "breaking_article_count": len(agent._state.breaking_news or []),
        "bucket_mismatch_count": len(bucket_mismatch),
        "market_with_related_count": len(market_with_related),
        "market_mentions_watchlist_count": len(market_mentions_watchlist),
        "multi_expected_coverage_fail_count": len(multi_expected_coverage_fail),
        "jmia_count": int(jmia.get("count", 0)),
        "jmia_suspect_count": int(jmia.get("suspect_count", 0)),
    }

    report = {
        "summary": summary,
        "selected_tickers": selected,
        "tickers": rows,
        "bucket_mismatch_samples": bucket_mismatch[:25],
        "market_with_related_samples": market_with_related[:25],
        "market_mentions_watchlist_samples": market_mentions_watchlist[:25],
        "multi_expected_coverage_fail_samples": multi_expected_coverage_fail[:25],
    }

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"news_live_rebuild_audit_{ts}.json"
    txt_path = out_dir / f"news_live_rebuild_audit_{ts}.txt"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = []
    lines.append("NEWS LIVE REBUILD AUDIT")
    for k, v in summary.items():
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("Top tickers (selected):")
    for r in rows:
        lines.append(
            f"- {r['ticker']}: count={r['count']}, suspect={r['suspect_count']}, "
            f"ratio={r['suspect_ratio']:.2%}, multi={r['multi_count']}"
        )
    lines.append("")
    lines.append("JMIA samples:")
    if int(jmia.get("count", 0)) == 0:
        lines.append("- no JMIA articles")
    else:
        jmia_articles = list((agent._state.ticker_news or {}).get("JMIA", []) or [])
        for a in jmia_articles[:10]:
            lines.append(f"- {a.title[:180]}")
    lines.append("")
    lines.append(f"JSON: {json_path}")
    lines.append(f"TXT: {txt_path}")
    txt_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "json_report": str(json_path),
        "txt_report": str(txt_path),
        "summary": summary,
    }


def main() -> int:
    result = run_audit()
    summary = result["summary"]
    print(f"JSON_REPORT={result['json_report']}")
    print(f"TXT_REPORT={result['txt_report']}")
    print(f"WATCHLIST={summary['watchlist_count']}")
    print(f"NEW_ARTICLES={summary['new_articles_count']}")
    print(f"JMIA_COUNT={summary['jmia_count']}")
    print(f"JMIA_SUSPECT={summary['jmia_suspect_count']}")
    print(f"BUCKET_MISMATCH={summary['bucket_mismatch_count']}")
    print(f"MARKET_WITH_RELATED={summary['market_with_related_count']}")
    print(f"MARKET_MENTIONS_WATCHLIST={summary['market_mentions_watchlist_count']}")
    print(f"MULTI_FAIL={summary['multi_expected_coverage_fail_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
