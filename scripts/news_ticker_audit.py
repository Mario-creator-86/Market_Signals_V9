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
    p = Path("watchlist.json")
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


def main() -> int:
    out_dir = Path("output") / "news_audit"
    out_dir.mkdir(parents=True, exist_ok=True)

    watchlist = load_watchlist()
    watchset = set(watchlist)

    agent = get_news_tab_agent()
    all_articles = agent._iter_all_articles()
    agent._organize_state(all_articles, watchlist=watchlist)
    agent._save_backup()

    bucket_counts = {tk: len(arr or []) for tk, arr in (agent._state.ticker_news or {}).items()}
    selected = sorted(bucket_counts, key=lambda k: bucket_counts[k], reverse=True)[:30]
    if "JMIA" in watchset and "JMIA" not in selected:
        if len(selected) >= 30:
            selected = selected[:29] + ["JMIA"]
        else:
            selected.append("JMIA")
    selected = list(dict.fromkeys(selected))

    id_to_buckets = defaultdict(set)
    for tk, arr in (agent._state.ticker_news or {}).items():
        for a in (arr or []):
            id_to_buckets[a.id].add(tk)

    rows = []
    for tk in selected:
        arr = list((agent._state.ticker_news or {}).get(tk, []) or [])
        suspect = []
        for a in arr:
            if not agent._is_ticker_mentioned_in_article(tk, a):
                suspect.append(a)
        multi = 0
        for a in arr:
            rel = set(a.related_tickers or [])
            if len(rel) >= 2:
                multi += 1
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
            market_mentions_watchlist.append(
                {
                    "id": a.id,
                    "title": a.title,
                    "mentions": mentions[:6],
                }
            )

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

    summary = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "watchlist_count": len(watchlist),
        "selected_tickers": selected,
        "selected_count": len(selected),
        "ticker_bucket_count": len((agent._state.ticker_news or {}).keys()),
        "ticker_article_total": sum(len(v or []) for v in (agent._state.ticker_news or {}).values()),
        "market_article_count": len(agent._state.market_news or []),
        "breaking_article_count": len(agent._state.breaking_news or []),
        "bucket_mismatch_count": len(bucket_mismatch),
        "market_with_related_count": len(market_with_related),
        "market_mentions_watchlist_count": len(market_mentions_watchlist),
        "multi_expected_coverage_fail_count": len(multi_expected_coverage_fail),
    }

    report = {
        "summary": summary,
        "tickers": rows,
        "bucket_mismatch_samples": bucket_mismatch[:20],
        "market_with_related_samples": market_with_related[:20],
        "market_mentions_watchlist_samples": market_mentions_watchlist[:20],
        "multi_expected_coverage_fail_samples": multi_expected_coverage_fail[:20],
    }

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"news_ticker_audit_{ts}.json"
    txt_path = out_dir / f"news_ticker_audit_{ts}.txt"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = []
    lines.append("NEWS TICKER ALLOCATION AUDIT")
    lines.append(f"Generated: {summary['timestamp']}")
    lines.append(f"Selected tickers: {summary['selected_count']} / Watchlist: {summary['watchlist_count']}")
    lines.append("")
    for k in [
        "ticker_bucket_count",
        "ticker_article_total",
        "market_article_count",
        "breaking_article_count",
        "bucket_mismatch_count",
        "market_with_related_count",
        "market_mentions_watchlist_count",
        "multi_expected_coverage_fail_count",
    ]:
        lines.append(f"{k}: {summary[k]}")
    lines.append("")
    lines.append("PER-TICKER (top 30 by bucket size, forced include JMIA):")
    for r in rows:
        lines.append(
            f"- {r['ticker']}: count={r['count']}, suspect={r['suspect_count']}, "
            f"suspect_ratio={r['suspect_ratio']:.2%}, multi={r['multi_count']}"
        )
        for s in r["sample_suspects"][:3]:
            lines.append(f"    * SUSPECT: {s['title'][:140]}")

    jmia = next((x for x in rows if x["ticker"] == "JMIA"), None)
    if jmia:
        lines.append("")
        lines.append(
            f"JMIA CHECK: count={jmia['count']}, suspect={jmia['suspect_count']}, "
            f"suspect_ratio={jmia['suspect_ratio']:.2%}"
        )

    lines.append("")
    lines.append(f"JSON: {json_path}")
    lines.append(f"TXT:  {txt_path}")
    txt_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"JSON_REPORT={json_path}")
    print(f"TXT_REPORT={txt_path}")
    print(f"JMIA_COUNT={(jmia['count'] if jmia else 0)}")
    print(f"JMIA_SUSPECT={(jmia['suspect_count'] if jmia else 0)}")
    print(f"BUCKET_MISMATCH={summary['bucket_mismatch_count']}")
    print(f"MARKET_WITH_RELATED={summary['market_with_related_count']}")
    print(f"MARKET_MENTIONS_WATCHLIST={summary['market_mentions_watchlist_count']}")
    print(f"MULTI_FAIL={summary['multi_expected_coverage_fail_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
