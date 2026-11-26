"""Point-in-time replay of one earnings week (read-only).

Takes every settled Pre-Earnings event in a week window and answers two
questions per event:

1. What did the system actually say BEFORE publication? (stored tier: the
   prediction registered pre-event in the feedback store — structured bias,
   stored AI verdict, top-prediction side. Zero look-ahead by construction.)
2. What would the CURRENT model say with access only to news published (and
   observed by our pipeline) up to the event? (replay tier: news cut at the
   event timestamp, fed through the production AI prompt via
   PreEarningsAgent._ollama_enrich_item with everything non-reconstructable
   left honestly absent.)

Both tiers are compared against the realized signed gap move already settled
in the feedback store. The script never sends orders, never mutates the
feedback store, live state, or caches, and reports abstentions and data gaps
instead of dropping them.

Known, unavoidable caveats (reported, not hidden):
- The replay LLM may carry post-event knowledge in its weights for well-known
  tickers (training-data lookahead). Rows are labeled accordingly.
- Analyst consensus, EPS beat history, eToro retail and gap risk are NOT
  point-in-time reconstructable and are left absent (N/A in the prompt).
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_pre_earnings_direction import (  # noqa: E402
    bias_side,
    finite,
    summarize,
    wilson,
)

DEFAULT_FEEDBACK = PROJECT_ROOT / "data" / "pre_earnings_feedback.json"
DEFAULT_NEWS = PROJECT_ROOT / "data" / "news_backup.json"
DEFAULT_LEDGER = PROJECT_ROOT / "data" / "news_evidence_ledger.jsonl"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "docs" / "product_audit_2026-09-06"

SIDE_TO_BIAS = {"LONG": 100, "SHORT": -100, "NEUTRAL": 0}

REPLAY_CAVEATS = [
    "llm_training_knowledge: the replay model may know post-event results for "
    "well-known tickers from its training data; treat replay verdicts as "
    "indicative, not as proof of point-in-time accuracy",
    "analyst_eps_retail_gap_absent: analyst consensus, EPS beat history, "
    "eToro retail and gap risk are not reconstructable as-of the event and "
    "were left absent from the replay prompt (rendered N/A)",
    "news_rotation: events whose pre-event news rotated out of the stores "
    "run without news evidence and are reported as abstentions, not drops",
]


def parse_dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def default_week_window(today=None):
    """The last complete Monday..Sunday week (UTC)."""
    current = (today or datetime.now(timezone.utc)).astimezone(timezone.utc)
    monday = (current - timedelta(days=current.weekday())).date()
    start = monday - timedelta(days=7)
    return (
        datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
        datetime(start.year, start.month, start.day, tzinfo=timezone.utc) + timedelta(days=7),
    )


def load_population(feedback, window):
    """Unique settled outcomes whose event date falls inside the window."""
    start, end = window
    rows = [row for row in (feedback.get("outcomes") or []) if isinstance(row, dict)]
    keys = Counter(
        (str(r.get("symbol") or "").upper(), str(r.get("event_utc") or "")[:10])
        for r in rows
    )
    population = []
    for row in rows:
        event_dt = parse_dt(row.get("event_utc"))
        key = (str(row.get("symbol") or "").upper(), str(row.get("event_utc") or "")[:10])
        if event_dt is None or not (start <= event_dt < end):
            continue
        if not key[0] or keys[key] != 1:
            continue  # ambiguous duplicates are excluded as a group (audit rule)
        population.append((event_dt, row))
    population.sort(key=lambda pair: (pair[0], pair[1].get("symbol") or ""))
    return [row for _, row in population]


def _norm_source(value):
    return re.sub(r"\s+", " ", str(value or "unknown")).strip().lower()


def _dedupe_key(url, title, source):
    url_norm = str(url or "").strip().lower()
    if url_norm.startswith("http"):
        return ("url", url_norm)
    return ("title", str(title or "").strip().lower(), _norm_source(source))


def collect_news(symbol, cutoff, ticker_news, ledger_rows, limit=12):
    """Point-in-time news for one event.

    Backup-store rows need published_at <= cutoff (no receipt timestamp is
    stored, so they are labeled with their weaker provenance). Ledger rows are
    held to the stricter rule published_at <= cutoff AND observed_at <= cutoff:
    observed_at is when our own pipeline first saw the article, so a late
    backfill can never pass as pre-event evidence.
    """
    backup_rows = [
        row for row in (ticker_news.get(symbol) or [])
        if isinstance(row, dict)
    ]
    ledger_for_symbol = [
        row for row in ledger_rows
        if isinstance(row, dict) and str(row.get("ticker") or "").upper().strip() == symbol
    ]
    merged, seen = [], set()
    stats = {"backup_pre": 0, "ledger_pre": 0, "dropped_after": 0}
    for provenance, rows, guard in (
        ("news_backup", backup_rows, "published"),
        ("news_ledger", ledger_for_symbol, "both"),
    ):
        for row in rows:
            published = parse_dt(row.get("published_at") or row.get("published_utc"))
            if published is None or published > cutoff:
                continue
            if guard == "both":
                observed = parse_dt(row.get("observed_at"))
                if observed is None or observed > cutoff:
                    continue
            stats[f"{provenance.split('_')[1]}_pre"] += 1
            key = _dedupe_key(row.get("url"), row.get("title"), row.get("source"))
            if key in seen:
                continue
            seen.add(key)
            merged.append({
                "title": str(row.get("title") or "").strip(),
                "source": str(row.get("source") or "unknown").strip(),
                "url": str(row.get("url") or "").strip(),
                "published_utc": published.isoformat(),
                "summary": str(row.get("summary") or row.get("ai_micro_summary") or "").strip(),
                "provenance": provenance,
            })
    merged.sort(key=lambda row: row["published_utc"], reverse=True)
    if len(merged) > limit:
        stats["dropped_after"] = len(merged) - limit
        merged = merged[:limit]
    return merged, stats


def historical_reactions(symbol, event_dt, outcomes, limit=3):
    """Settled reactions of the same symbol for events strictly BEFORE this
    one — legitimately pre-event knowledge."""
    rows = []
    for row in outcomes:
        other_dt = parse_dt(row.get("event_utc"))
        if (
            row is not None
            and str(row.get("symbol") or "").upper().strip() == symbol
            and other_dt is not None
            and other_dt < event_dt
        ):
            move = finite(row.get("realized_signed_move_pct"))
            if move is not None:
                rows.append((other_dt, move))
    rows.sort()
    return [
        {"date": dt.date().isoformat(), "move_pct": round(move, 2)}
        for dt, move in rows[-limit:]
    ]


def build_replay_item(outcome, news, reactions):
    """Synthetic Pre-Earnings item holding only point-in-time data. Fields the
    production prompt expects but replay cannot reconstruct are simply absent
    (the prompt renders them N/A)."""
    sources = {row.get("source", "").lower() for row in news if row.get("source")}
    return {
        "symbol": str(outcome.get("symbol") or "").upper(),
        "earnings_time_utc": outcome.get("event_utc"),
        "session": outcome.get("session"),
        "sector": outcome.get("sector"),
        "top_news": news,
        "news_digest": {"article_count": len(news), "unique_sources": len(sources)},
        "recent_reactions": reactions,
        "replay": True,
    }


def default_ai_fn(model, timeout_sec):
    """Production path: the agent's own per-symbol prompt/parser via Ollama."""
    agent = None

    def ai_fn(item):
        nonlocal agent
        if agent is None:
            from agents.pre_earnings_agent import PreEarningsAgent

            agent = PreEarningsAgent()
        return agent._ollama_enrich_item(item, model, timeout_sec)

    return ai_fn


def replay_row(outcome, ai_fn, ticker_news, ledger_rows, *, min_news, news_limit):
    """Assemble one report row: stored call, cut news, replay call, outcome."""
    symbol = str(outcome.get("symbol") or "").upper()
    event_dt = parse_dt(outcome.get("event_utc"))
    news, news_stats = collect_news(
        symbol, event_dt, ticker_news, ledger_rows, limit=news_limit
    )
    evidence = outcome.get("first_direction_evidence")
    stored = {
        "label": bias_side(outcome),
        "bias": finite(outcome.get("predicted_direction_bias")),
        "ai_verdict": outcome.get("predicted_ai_verdict"),
        "ai_confidence": finite(outcome.get("predicted_ai_confidence")),
        "gap_risk": finite(outcome.get("predicted_gap_risk")),
        "was_top_prediction": outcome.get("was_top_prediction") is True,
        "top_side": outcome.get("top_prediction_direction"),
        "frozen_evidence_quality": (
            evidence.get("capture_quality") if isinstance(evidence, dict) else None
        ),
    }
    row = {
        "symbol": symbol,
        "event_utc": outcome.get("event_utc"),
        "session": outcome.get("session"),
        "sector": outcome.get("sector"),
        "realized_signed_move_pct": finite(outcome.get("realized_signed_move_pct")),
        "stored": stored,
        "news": {"cutoff_utc": event_dt.isoformat(), **news_stats, "used": len(news)},
        "replay": None,
    }
    if len(news) < min_news:
        row["replay_skipped_reason"] = f"pre_event_news_below_{min_news}"
        return row
    if ai_fn is None:
        row["replay_skipped_reason"] = "ai_disabled"
        return row
    reactions = historical_reactions(symbol, event_dt, outcomes_source[0])
    item = build_replay_item(outcome, news, reactions)
    try:
        enriched = ai_fn(item)
    except Exception as exc:  # a failed call is an abstention, never a fake verdict
        row["replay"] = {"error": str(exc)[:160]}
        return row
    if not isinstance(enriched, dict):
        row["replay"] = {"error": "EMPTY_RESPONSE"}
        return row
    if enriched.get("_error"):
        row["replay"] = {"error": str(enriched.get("_error"))}
        return row
    row["replay"] = {
        "decision": str(enriched.get("decision") or "NEUTRAL").strip().upper(),
        "decision_confidence": finite(enriched.get("decision_confidence")),
        "sentiment": finite(enriched.get("sentiment")),
        "catalyst_label": enriched.get("catalyst_label"),
        "rationale": str(enriched.get("decision_rationale") or "").strip()[:280],
        "lookahead_caveats": ["llm_training_knowledge"],
    }
    return row


def summarize_tier(rows, side_fn, key=None):
    """Audit-compatible metrics over (optionally filtered) rows."""
    if key is not None:
        rows = [row for row in rows if key(row)]
    return summarize(rows, side_fn)


def agreement(stored_rows, replay_rows):
    """Stored AI verdict vs replay decision on rows where both are directional."""
    both = []
    for stored, replay in zip(stored_rows, replay_rows):
        stored_side = stored.get("stored", {}).get("ai_verdict")
        replay_side = (replay.get("replay") or {}).get("decision")
        if stored_side in {"LONG", "SHORT"} and replay_side in {"LONG", "SHORT"}:
            both.append((stored_side, replay_side))
    if not both:
        return {"n": 0, "agreement_pct": None}
    same = sum(1 for a, b in both if a == b)
    return {
        "n": len(both),
        "agreement_pct": round(100 * same / len(both), 2),
        "wilson_95_pct_descriptive": wilson(same, len(both)),
    }


outcomes_source = [()]  # set by run_replay; historical_reactions reads it


def run_replay(feedback, ticker_news, ledger_rows, window, *, ai_fn=None,
               min_news=1, news_limit=12, previous_rows=None):
    population = load_population(feedback, window)
    outcomes_source[0] = feedback.get("outcomes") or []
    done = set()
    for row in previous_rows or []:
        if row.get("replay") or row.get("replay_skipped_reason") == "ai_disabled":
            done.add((row.get("symbol"), str(row.get("event_utc") or "")[:10]))
    rows = list(previous_rows or [])
    for outcome in population:
        symbol = str(outcome.get("symbol") or "").upper()
        key = (symbol, str(outcome.get("event_utc") or "")[:10])
        if key in done:
            continue
        rows.append(replay_row(
            outcome, ai_fn, ticker_news, ledger_rows,
            min_news=min_news, news_limit=news_limit,
        ))
    report = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window": {"start": window[0].isoformat(), "end": window[1].isoformat()},
        "population": len(population),
        "method": {
            "stored_tier": "prediction registered in the feedback store before "
                           "the event (no look-ahead by construction)",
            "replay_tier": "production AI prompt re-run on news with "
                           "published_at <= event (ledger rows additionally "
                           "require observed_at <= event); non-reconstructable "
                           "inputs left absent",
            "ground_truth": "realized signed gap move settled by the feedback "
                            "pipeline (~36h after the report)",
            "caveats": REPLAY_CAVEATS,
        },
        "config": {
            "min_news": min_news,
            "news_limit": news_limit,
            "ai_enabled": ai_fn is not None,
        },
        "rows": rows,
    }
    finalize_report(report)
    return report


def finalize_report(report):
    """Attach the tier metrics + news-coverage stats to the report."""
    rows = report["rows"]
    report["news_coverage"] = {
        "events_with_zero_pre_event_articles": sum(1 for r in rows if r["news"]["used"] == 0),
        "events_with_1_2_articles": sum(1 for r in rows if 1 <= r["news"]["used"] <= 2),
        "events_with_3plus_articles": sum(1 for r in rows if r["news"]["used"] >= 3),
        "median_pre_event_articles_used": (
            sorted(r["news"]["used"] for r in rows)[len(rows) // 2] if rows else 0
        ),
    }
    report["stored_bias_tier"] = summarize_tier(rows, lambda r: r["stored"]["label"])
    report["stored_ai_tier"] = summarize_tier(
        rows, lambda r: r["stored"]["ai_verdict"],
        key=lambda r: r["stored"]["ai_verdict"] in {"LONG", "SHORT", "NEUTRAL"},
    )
    report["top_predictions_tier"] = summarize_tier(
        rows, lambda r: r["stored"]["top_side"],
        key=lambda r: r["stored"]["was_top_prediction"],
    )
    report["replay_tier"] = summarize_tier(
        rows,
        lambda r: (r.get("replay") or {}).get("decision"),
        key=lambda r: bool(r.get("replay")) and not r["replay"].get("error"),
    )
    report["monthly"] = monthly_breakdown(rows)
    replayed = [r for r in rows if r.get("replay") and not r["replay"].get("error")]
    report["stored_vs_replay_ai_agreement"] = agreement(rows, rows)
    report["replay_errors"] = sum(1 for r in rows if (r.get("replay") or {}).get("error"))
    return report


def monthly_breakdown(rows):
    """Per-calendar-month stored tiers — stability across periods is what
    justifies or kills a policy, not one lucky month."""
    per_month = {}
    for row in rows:
        month = str(row.get("event_utc") or "")[:7]
        if not month:
            continue
        per_month.setdefault(month, []).append(row)
    return {
        month: {
            "events": len(rows_month),
            "stored_bias_tier": summarize_tier(rows_month, lambda r: r["stored"]["label"]),
            "stored_ai_tier": summarize_tier(rows_month, lambda r: r["stored"]["ai_verdict"]),
        }
        for month, rows_month in sorted(per_month.items())
    }


def print_summary(report):
    window = report["window"]
    print(f"Replay window {window['start'][:10]} .. {window['end'][:10]} "
          f"- {report['population']} events")
    for name in ("stored_bias_tier", "stored_ai_tier", "top_predictions_tier", "replay_tier"):
        tier = report.get(name) or {}
        print(
            f"{name}: calls={tier.get('directional_nonflat_calls')} "
            f"hits={tier.get('hits')} acc={tier.get('accuracy_pct')}% "
            f"wilson={tier.get('wilson_95_pct_descriptive')} "
            f"alwaysLONG={tier.get('always_long_accuracy_same_cases_pct')}% "
            f"L/S={tier.get('long_calls')}/{tier.get('short_calls')} "
            f"excluded={tier.get('excluded')}"
        )
    print(f"news coverage: {report['news_coverage']}")
    print(f"stored vs replay AI agreement: {report['stored_vs_replay_ai_agreement']}")
    print(f"replay errors: {report['replay_errors']}")
    for month, stats in (report.get("monthly") or {}).items():
        bias = stats["stored_bias_tier"]
        ai = stats["stored_ai_tier"]
        print(
            f"  {month}: ev={stats['events']} "
            f"bias {bias.get('hits')}/{bias.get('directional_nonflat_calls')} "
            f"={bias.get('accuracy_pct')}% (LONG-ref {bias.get('always_long_accuracy_same_cases_pct')}%) "
            f"ai {ai.get('hits')}/{ai.get('directional_nonflat_calls')} "
            f"={ai.get('accuracy_pct')}% (LONG-ref {ai.get('always_long_accuracy_same_cases_pct')}%)"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--feedback", default=str(DEFAULT_FEEDBACK))
    parser.add_argument("--news", default=str(DEFAULT_NEWS))
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--week-start", default=None, help="YYYY-MM-DD (Monday)")
    parser.add_argument("--week-end", default=None, help="YYYY-MM-DD exclusive")
    parser.add_argument("--limit", type=int, default=0, help="cap events (0 = all)")
    parser.add_argument("--min-news", type=int, default=1,
                        help="skip replay AI below this many pre-event articles")
    parser.add_argument("--news-limit", type=int, default=12)
    parser.add_argument("--no-ai", action="store_true",
                        help="stored tiers + news coverage only (no Ollama calls)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--ai-timeout", type=int, default=60)
    parser.add_argument("--output", default=None)
    parser.add_argument("--resume", action="store_true",
                        help="reuse rows already decided in the output file")
    args = parser.parse_args(argv)

    if args.week_start and args.week_end:
        window = (
            parse_dt(args.week_start),
            parse_dt(args.week_end),
        )
    else:
        window = default_week_window()
    if not window[0] or not window[1] or not (window[1] > window[0]):
        parser.error("need a valid --week-start/--week-end pair")

    feedback = json.loads(Path(args.feedback).read_text(encoding="utf-8"))
    ticker_news = {}
    news_path = Path(args.news)
    if news_path.exists():
        ticker_news = (json.loads(news_path.read_text(encoding="utf-8")) or {}).get("ticker_news") or {}
    ledger_rows = []
    ledger_path = Path(args.ledger)
    if ledger_path.exists():
        with ledger_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        ledger_rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

    output_path = Path(args.output) if args.output else (
        DEFAULT_OUTPUT_DIR / f"replay_week_{window[0].date()}_{window[1].date()}.json"
    )
    previous_rows = []
    if args.resume and output_path.exists():
        try:
            previous_rows = json.loads(output_path.read_text(encoding="utf-8")).get("rows") or []
        except (json.JSONDecodeError, OSError):
            previous_rows = []

    ai_fn = None
    if not args.no_ai:
        ai_fn = default_ai_fn(args.model, args.ai_timeout)

    population = load_population(feedback, window)
    outcomes_source[0] = feedback.get("outcomes") or []
    rows = list(previous_rows) if args.resume else []
    done = {
        (r.get("symbol"), str(r.get("event_utc") or "")[:10])
        for r in rows if r.get("replay") or r.get("replay_skipped_reason")
    }
    report = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window": {"start": window[0].isoformat(), "end": window[1].isoformat()},
        "population": len(population),
        "method": {
            "stored_tier": "prediction registered in the feedback store before "
                           "the event (no look-ahead by construction)",
            "replay_tier": "production AI prompt re-run on news with "
                           "published_at <= event (ledger rows additionally "
                           "require observed_at <= event); non-reconstructable "
                           "inputs left absent",
            "ground_truth": "realized signed gap move settled by the feedback "
                            "pipeline (~36h after the report)",
            "caveats": REPLAY_CAVEATS,
        },
        "config": {
            "min_news": args.min_news,
            "news_limit": args.news_limit,
            "ai_enabled": ai_fn is not None,
            "model": args.model,
        },
        "rows": rows,
    }

    def save():
        finalize_report(report)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8"
        )

    processed = 0
    for outcome in population:
        key = (str(outcome.get("symbol") or "").upper(), str(outcome.get("event_utc") or "")[:10])
        if args.resume and key in done:
            continue
        rows.append(replay_row(
            outcome, ai_fn, ticker_news, ledger_rows,
            min_news=args.min_news, news_limit=args.news_limit,
        ))
        done.add(key)
        processed += 1
        if args.limit and processed >= args.limit:
            break
        if processed % 5 == 0:
            save()  # incremental checkpoint: a long AI run must survive interruption

    save()
    print_summary(report)
    print(f"report: {output_path}")
    return report


if __name__ == "__main__":
    main()
